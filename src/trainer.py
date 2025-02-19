import os
import pickle
from tqdm import tqdm
import imageio
import glob
from src.models import Model
import jax
import jax.numpy as np
from jax import jit, random
from jax.experimental import optimizers
from livelossplot import PlotLosses
import matplotlib.pyplot as plt
import numpy as onp
from transformers import FlaxCLIPModel
import flax

from src.step_utils import render_fn, psnr_fn, mse_fn, single_step, CLIPProcessor, random_pose
from src.data_utils import poses_avg, render_path_spiral, get_rays, data_loader

class Trainer:

    def __init__(self, args):
        self.args = args
        self.model = Model()
        key1, key2 = random.split(jax.random.PRNGKey(0))
        dummy_x = random.normal(key1, (1, 3))
        self.params = self.model.init(key2, dummy_x)

        if args.pretrained is not None:
            self.params = flax.core.frozen_dict.unfreeze(self.params)
            with open(os.path.join(self.args.datadir, self.args.select_data, self.args.pretrained), 'rb') as file:
                pretrained = pickle.load(file)
            for i, (_, p) in enumerate(pretrained.items()):
                self.params['params']['fc%s'%(str(i) if i < 5 else '_last')]['kernel'] = p['w']
                self.params['params']['fc%s'%(str(i) if i < 5 else '_last')]['bias'] = p['b']
            self.params = flax.core.frozen_dict.freeze(self.params)
            
        self.CLIP_model = FlaxCLIPModel.from_pretrained("openai/clip-vit-base-patch32", dtype = np.float16)

        opt_init, self.opt_update, self.get_params = optimizers.adam(args.lr)
        self.opt_state = opt_init(self.params)
        self.loss = 1e5
        
        self.imgdata, self.embeded_imgdata, self.posedata = data_loader(args.select_data, args.datadir, self.CLIP_model)

        self.total_num_of_sample = len(self.imgdata['train']) + len(self.imgdata['test']) + len(self.imgdata['val'])
        print(f'{self.total_num_of_sample} images')
        print('Pose data loaded - ', self.posedata.keys())

        ## CLIPS things
        self.K = 16

    def update_network_weights(self, rng, step, images, rays, params, inner_steps, bds, target_emb):
        H, W, _ = images.shape
        downsample = max(H,W)//56
        i, j = np.meshgrid(np.arange(0, W, downsample), np.arange(0, H, downsample), indexing='xy')
        f = H * 1.
        kinv = np.array([
            1. / f, 0, -W * .5 / f,
            0, -1. / f, H * .5 / f,
            0, 0, -1.
        ]).reshape([3, 3])
        images_ = np.reshape(images, (-1, 3))

        for _ in range(inner_steps):
            rng, rng_input = random.split(rng)
            random_ray = get_rays(random_pose(rng, bds), kinv, i, j)
            idx = random.randint(rng_input, shape=(self.args.batch_size,), minval=0, maxval=images_.shape[0])
            image_sub = images_[idx, :]
            rays_sub = rays[:, idx, :]
            rng, params, loss = single_step(rng, step, image_sub, rays_sub, params, bds, self.args.inner_step_size, self.args.N_samples, self.model, 
                random_ray, target_emb, self.CLIP_model, self.K) # arguments for sc_loss
        return rng, params, loss

    def update_model(self, step, rng, params, opt_state, image, rays, bds, target_emb):
        rng, new_params, model_loss = self.update_network_weights(rng, step, image, rays, params, self.args.inner_update_steps, bds, target_emb)

        def calc_grad(params, new_params):
            return params - new_params

        model_grad = jax.tree_multimap(calc_grad, params, new_params)
        opt_state = self.opt_update(step, model_grad, opt_state)
        params = self.get_params(opt_state)
        return rng, params, opt_state, model_loss

    @jit
    def update_model_single(self, step, rng, params, opt_state, image, rays, bds, random_ray, target_emb):

        def calc_grad(params, new_params):
            return params - new_params

        rng, new_params, model_loss = single_step(rng, image, rays, params, bds, self.args.inner_step_size, self.args.N_samples, self.model, 
            random_ray, target_emb, self.CLIP_model, self.K) # arguments for sc_loss
        model_grad = jax.tree_multimap(calc_grad, params, new_params)
        opt_state = self.opt_update(step, model_grad, opt_state)
        params = self.get_params(opt_state)
        return rng, params, opt_state, model_loss

    def get_example(self, img_idx, split='train', downsample=4):
        sc = .05

        img = self.imgdata[split][img_idx]
        # (4, 4)
        c2w =  self.posedata[split]['c2w_mats'][img_idx]
        # (3, 3)
        kinv = self.posedata[split]['kinv_mats'][img_idx]
        c2w = np.concatenate([c2w[:3 ,:3], c2w[:3 ,3:4 ] * sc], -1)
        # (2, )
        bds = self.posedata[split]['bds'][img_idx] * np.array([.9, 1.2]) * sc

        H, W = img.shape[:2]
        # (0, 4, 8, ..., H)
        i, j = np.meshgrid(np.arange(0, W, downsample), np.arange(0, H, downsample), indexing='xy')

        #test_images = img[j,i]
        test_rays = get_rays(c2w, kinv, i, j)

        embeded_test_images = self.embeded_imgdata[split][img_idx]

        return img[::downsample, ::downsample], embeded_test_images, test_rays, bds

    def train(self):
        step = 0
        rng = jax.random.PRNGKey(0)

        train_psnrs = []
        train_steps = []
        train_psnrs_all = []
        test_steps = []
        test_psnrs_all = []

        exp_name = f'{self.args.scene}_ius_{self.args.inner_update_steps}_ilr_{self.args.inner_step_size}_olr_{self.args.lr}_bs_{self.args.batch_size}'
        exp_dir = f'checkpoint/' + self.args.dataset + '_' + self.args.scene + '_checkpoints/{exp_name}/'
        temp_eval_result_dir = f'temp/temp_eval_result_dir/{exp_name}/'
        plt_groups = {'Train PSNR': [], 'Test PSNR': []}
        plotlosses_model = PlotLosses(groups=plt_groups)
        plt_groups['Train PSNR'].append(exp_name + f'_train')
        plt_groups['Test PSNR'].append(exp_name + f'_test')

        os.makedirs(exp_dir, exist_ok=True)
        os.makedirs(temp_eval_result_dir, exist_ok=True)

        for step in tqdm(range(self.args.max_iters)):
            try:
                rng, rng_input = random.split(rng)
                img_idx = random.randint(rng_input, shape=(), minval=0, maxval=self.total_num_of_sample - 25)
                images, embeded_images, rays, bds = self.get_example(img_idx, downsample=2)
                images /= 255.
            except:
                print('data loading error')
                raise

            target_emb = embeded_images

            rays = np.reshape(rays, (2, -1, 3))

            # don't need single
            rng, self.params, self.opt_state, self.loss = self.update_model(step, rng, self.params, self.opt_state, images, rays, bds,
                target_emb)

            train_psnrs.append(-10 * np.log10(self.loss))

            if step % 250 == 0:
                plotlosses_model.update({exp_name + '_train': np.mean(np.array(train_psnrs))}, current_step=step)
                train_steps.append(step)
                train_psnrs_all.append(np.mean(np.array(train_psnrs)))
                train_psnrs = []

            if step % 500 == 0 and step != 0:
                test_psnr = []
                for ti in range(5):
                    # TODO need pack into test image loader, need to to change Only Use Fewshot
                    test_images, _, test_rays, bds = self.get_example(ti, split='val', downsample=4)
                    test_images/=255

                    test_images, test_holdout_images = np.split(test_images, [test_images.shape[1] // 2], axis=1)
                    test_rays, test_holdout_rays = np.split(test_rays, [test_rays.shape[2] // 2], axis=2)

                    # Training Fewshot image
                    rng, test_params, test_inner_loss = self.update_network_weights(rng, 1, test_images, np.reshape(test_rays, (2, -1, 3)), self.params,
                                                                             self.args.test_inner_steps, bds, None)

                    # Rendering part
                    test_result = render_fn(rng, self.model, test_params, np.reshape(test_holdout_rays, (2, -1, 3)), bds[0], bds[1], self.args.N_samples)
                    test_result = np.reshape(test_result, test_holdout_rays.shape[1:])
                    test_psnr.append(psnr_fn(test_holdout_images, test_result))
                test_psnr = np.mean(np.array(test_psnr))

                test_steps.append(step)
                test_psnrs_all.append(test_psnr)

                plotlosses_model.update({exp_name + '_test': test_psnr}, current_step=step)
                plotlosses_model.send()

                plt.figure(figsize=(15, 5))
                plt.subplot(1, 3, 1)
                plt.imshow(test_images)
                plt.subplot(1, 3, 2)
                plt.imshow(test_holdout_images)
                plt.subplot(1, 3, 3)
                plt.imshow(test_result)
                plt.savefig(os.path.join(temp_eval_result_dir, "{:06d}.png".format(step)))

                plt.plot(train_steps, train_psnrs_all)
                plt.savefig(f'{exp_dir}train_curve_{step}.png')

                plt.plot(test_steps, test_psnrs_all)
                plt.savefig(f'{exp_dir}test_curve_{step}.png')

            if step % 10000 == 0 and step != 0:
                test_images, _, test_rays, bds = self.get_example(0, split='test')
                test_images/=255
                test_rays = np.reshape(test_rays, (2, -1, 3))
                # training 1
                rng, test_params_1, test_inner_loss = self.update_network_weights(rng, 1, test_images, test_rays, self.params,
                                                                             self.args.test_inner_steps, bds, None)

                test_images, _, test_rays, bds = self.get_example(1, split='test')
                test_images/=255
                test_images_flat = np.reshape(test_images, (-1, 3))
                test_rays = np.reshape(test_rays, (2, -1, 3))
                # training 2
                rng, test_params_2, test_inner_loss = self.update_network_weights(rng, 1, test_images, test_rays, self.params,
                                                                             self.args.test_inner_steps, bds, None)

                # if 'nerf_synthetic' in self.args.select_data:
                poses = self.posedata['test']['c2w_mats']
                c2w = poses_avg(poses)
                # TODO Need to change this rendering info with different dataset
                focal = .8
                # TODO Please consider this function when sampling a spherical pose
                render_poses = render_path_spiral(c2w, c2w[:3, 1], [.1, .1, .05], focal, zrate=.5, rots=2, N=120)

                bds = np.array([5., 25.]) * .05
                H = 128
                W = H * 3 // 2
                f = H * 1.
                kinv = np.array([
                    1. / f, 0, -W * .5 / f,
                    0, -1. / f, H * .5 / f,
                    0, 0, -1.
                ]).reshape([3, 3])
                i, j = np.meshgrid(np.arange(0, W), np.arange(0, H), indexing='xy')
                renders = []
                for p, c2w in enumerate(tqdm(render_poses)):
                    rays = get_rays(c2w, kinv, i, j)
                    interp = p / len(render_poses)
                    # TODO need to check this interp_params
                    interp_params = jax.tree_multimap(
                        lambda x, y: y * p / len(render_poses) + x * (1 - p / len(render_poses)),
                        test_params_1, test_params_2)
                    result = render_fn(rng, self.model, interp_params, np.reshape(rays,[2,-1,3]), bds[0], bds[1], self.args.N_samples)
                    result = np.reshape(result, rays.shape[1:])
                    renders.append(onp.array(result*255).astype(np.uint8))

                imageio.mimwrite(f'{exp_dir}render_sprial_{step}.mp4', renders, fps=30, quality=8)

                with open(f'{exp_dir}checkpount_{step}.pkl', 'wb') as file:
                    pickle.dump(self.params, file)
