"""Class managing the model inference

"""
import os
import random
import datetime
import json
import numpy as np
from tqdm import tqdm
import scipy.stats
import torch
import torchvision.transforms as transforms
from torch.utils.data.sampler import WeightedRandomSampler, SubsetRandomSampler
from torch_geometric.data import DataLoader
from n2j.trainval_data.graphs.cosmodc2_graph import CosmoDC2Graph
import n2j.models as models
from n2j.trainval_data.utils.transform_utils import Standardizer, Slicer
import n2j.inference.infer_utils as iutils
import matplotlib.pyplot as plt
import corner


def get_idx(orig_list, sub_list):
    idx = []
    for item in sub_list:
        idx.append(orig_list.index(item))
    return idx


class InferenceManager:

    def __init__(self, device_type, checkpoint_dir, out_dir, seed=123):
        """Inference tool

        Parameters
        ----------
        device_type : str
        checkpoint_dir : os.path or str
            training checkpoint_dir (same as one used to instantiate `Trainer`)
        out_dir : os.path or str
            output directory for inference results

        """
        self.device_type = device_type
        self.device = torch.device(self.device_type)
        self.seed = seed
        self.seed_everything()
        self.checkpoint_dir = checkpoint_dir
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        self.out_dir = out_dir
        os.makedirs(self.out_dir, exist_ok=True)

    def seed_everything(self):
        """Seed the training and sampling for reproducibility

        """
        np.random.seed(self.seed)
        random.seed(self.seed)
        torch.manual_seed(self.seed)
        torch.cuda.manual_seed(self.seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    def load_dataset(self, data_kwargs, is_train, batch_size,
                     sub_features=None, sub_target=None, sub_target_local=None,
                     rebin=False, num_workers=2):
        """Load training and test datasets

        """
        # TODO: val should be test
        self.batch_size = batch_size
        self.num_workers = num_workers
        # X metadata
        features = data_kwargs['features']
        self.sub_features = sub_features if sub_features else features
        self.X_dim = len(self.sub_features)
        # Global y metadata
        target = ['final_kappa', 'final_gamma1', 'final_gamma2']
        self.sub_target = sub_target if sub_target else target
        self.Y_dim = len(self.sub_target)
        # Lobal y metadata
        target_local = ['halo_mass', 'stellar_mass', 'redshift']
        self.sub_target_local = sub_target_local if sub_target_local else target_local
        self.Y_local_dim = len(self.sub_target_local)
        dataset = CosmoDC2Graph(**data_kwargs)
        if is_train:
            self.train_dataset = dataset
            if os.path.exists(os.path.join(self.checkpoint_dir, 'stats.pt')):
                stats = torch.load(os.path.join(self.checkpoint_dir, 'stats.pt'))
            else:
                stats = self.train_dataset.data_stats
                torch.save(stats, os.path.join(self.checkpoint_dir, 'stats.pt'))
            # Transforming X
            if sub_features:
                idx = get_idx(features, sub_features)
                self.X_mean = stats['X_mean'][:, idx]
                self.X_std = stats['X_std'][:, idx]
                slicing = Slicer(idx)
                norming = Standardizer(self.X_mean, self.X_std)
                self.transform_X = transforms.Compose([slicing, norming])
            else:
                self.X_mean = stats['X_mean']
                self.X_std = stats['X_std']
                self.transform_X = Standardizer(self.X_mean, self.X_std)
            # Transforming global Y
            if sub_target:
                idx_Y = get_idx(target, sub_target)
                self.Y_mean = stats['Y_mean'][:, idx_Y]
                self.Y_std = stats['Y_std'][:, idx_Y]
                slicing_Y = Slicer(idx_Y)
                norming_Y = Standardizer(self.Y_mean, self.Y_std)
                self.transform_Y = transforms.Compose([slicing_Y, norming_Y])
            else:
                self.transform_Y = Standardizer(self.Y_mean, self.Y_std)
            # Transforming local Y
            if sub_target_local:
                idx_Y_local = get_idx(target_local, sub_target_local)
                self.Y_local_mean = stats['Y_local_mean'][:, idx_Y_local]
                self.Y_local_std = stats['Y_local_std'][:, idx_Y_local]
                slicing_Y_local = Slicer(idx_Y_local)
                norming_Y_local = Standardizer(self.Y_local_mean, self.Y_local_std)
                self.transform_Y_local = transforms.Compose([slicing_Y_local,
                                                            norming_Y_local])
            else:
                self.transform_Y_local = Standardizer(self.Y_local_mean, self.Y_local_std)
            self.train_dataset.transform_X = self.transform_X
            self.train_dataset.transform_Y = self.transform_Y
            self.train_dataset.transform_Y_local = self.transform_Y_local
            # Loading option 1: Subsample from a distribution
            if data_kwargs['subsample_pdf_func'] is not None:
                self.class_weight = None
                sampler = SubsetRandomSampler(stats['subsample_idx'])
                self.train_loader = DataLoader(self.train_dataset,
                                               batch_size=self.batch_size,
                                               sampler=sampler,
                                               num_workers=self.num_workers,
                                               drop_last=True)
            else:
                # Loading option 2: Over/undersample according to inverse frequency
                if rebin:
                    self.class_weight = stats['class_weight']
                    sampler = WeightedRandomSampler(stats['y_weight'],
                                                    num_samples=len(self.train_dataset))
                    self.train_loader = DataLoader(self.train_dataset,
                                                   batch_size=self.batch_size,
                                                   sampler=sampler,
                                                   num_workers=self.num_workers,
                                                   drop_last=True)
                # Loading option 3: No special sampling, just shuffle
                else:
                    self.class_weight = None
                    self.train_loader = DataLoader(self.train_dataset,
                                                   batch_size=self.batch_size,
                                                   shuffle=True,
                                                   num_workers=self.num_workers,
                                                   drop_last=True)
        else:
            self.val_dataset = dataset
            self.val_dataset.transform_X = self.transform_X
            self.val_dataset.transform_Y = self.transform_Y
            self.val_dataset.transform_Y_local = self.transform_Y_local
            self.val_loader = DataLoader(self.val_dataset,
                                         batch_size=self.batch_size,
                                         shuffle=False,
                                         num_workers=self.num_workers,
                                         drop_last=True)

    def reset_val_dataset(self, subsample_pdf_func, n_val):
        """Reset val loader to follow the specified distribution

        """
        rng = np.random.default_rng(123)
        y_val = self.get_true_kappa(is_train=False, add_suffix='orig').squeeze()
        subsample_idx_path = os.path.join(self.out_dir, 'subsample_idx.npy')
        if os.path.exists(subsample_idx_path):
            subsample_idx = np.load(subsample_idx_path).tolist()
        else:
            print("Evaluating the resampling density...")
            print(f"on test set of size {len(y_val)}")
            kde = scipy.stats.gaussian_kde(y_val, bw_method='scott')
            p = subsample_pdf_func(y_val)/kde.pdf(y_val)
            p /= np.sum(p)
            subsample_idx = rng.choice(np.arange(len(y_val)),
                                       p=p, replace=False, size=n_val)
            subsample_idx = subsample_idx.tolist()
            np.save(subsample_idx_path, subsample_idx)
        val_subset = torch.utils.data.Subset(self.val_dataset, subsample_idx)
        self.val_dataset = val_subset
        self.val_loader = DataLoader(self.val_dataset,
                                     batch_size=self.batch_size,
                                     shuffle=False,
                                     num_workers=self.num_workers,
                                     drop_last=False)
        print(f"The test dataset now has size {n_val}")

    def configure_model(self, model_name, model_kwargs={}):
        self.model_name = model_name
        self.model_kwargs = model_kwargs
        self.model = getattr(models, model_name)(**self.model_kwargs)
        self.model.to(self.device)
        if self.class_weight is not None:
            self.model.class_weight = self.class_weight.to(self.device)
        n_params = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        print(f"Number of params: {n_params}")

    def load_state(self, state_path):
        """Load the state dict of the past training

        Parameters
        ----------
        state_path : str or os.path object
            path of the state dict to load

        """
        state = torch.load(state_path)
        self.model.load_state_dict(state['model'])
        self.model.to(self.device)
        self.epoch = state['epoch']
        train_loss = state['train_loss']
        val_loss = state['val_loss']
        print("Loaded weights at {:s}".format(state_path))
        print("Epoch [{}]: TRAIN Loss: {:.4f}".format(self.epoch, train_loss))
        print("Epoch [{}]: VALID Loss: {:.4f}".format(self.epoch, val_loss))
        self.last_saved_val_loss = val_loss

    def get_bnn_kappa(self, n_samples=50, n_mc_dropout=20):
        """Get the samples from the BNN

        Parameters
        ----------
        n_samples : int
            number of samples per MC iterate
        n_mc_dropout : int
            number of MC iterates

        Returns
        -------
        np.array of shape `[n_test, self.Y_dim, n_samples*n_mc_dropout]`

        """
        path = os.path.join(self.out_dir, 'k_bnn.npy')
        if os.path.exists(path):
            samples = np.load(path)
            return samples
        # Fetch precomputed Y_mean, Y_std to de-standardize samples
        Y_mean = self.Y_mean.to(self.device)
        Y_std = self.Y_std.to(self.device)
        n_test = len(self.val_dataset)
        self.model.eval()
        with torch.no_grad():
            samples = np.empty([n_test, n_mc_dropout, n_samples, self.Y_dim])
            for i, batch in enumerate(self.val_loader):
                batch = batch.to(self.device)
                for mc_iter in range(n_mc_dropout):
                    x, u = self.model(batch)
                    B = u.shape[0]  # [this batch size]
                    # Get pred samples for this MC iterate
                    self.model.global_nll.set_trained_pred(u)
                    mc_samples = self.model.global_nll.sample(Y_mean,
                                                              Y_std,
                                                              n_samples)
                    samples[i*B: (i+1)*B, mc_iter, :, :] = mc_samples
        samples = samples.transpose(0, 3, 1, 2).reshape([n_test, self.Y_dim, -1])
        np.save(path, samples)
        return samples

    def get_true_kappa(self, is_train, add_suffix='', save=True):
        # Init k_train
        if is_train:
            loader = self.train_loader
            suffix = 'train'
        else:
            loader = self.val_loader
            suffix = 'val'
        path = os.path.join(self.out_dir, f'k_{suffix}{add_suffix}.npy')
        if os.path.exists(path):
            true_kappa = np.load(path)
            return true_kappa
        # Fetch precomputed Y_mean, Y_std to de-standardize samples
        Y_mean = self.Y_mean.to(self.device)
        Y_std = self.Y_std.to(self.device)
        n_test = len(self.val_dataset)
        true_kappa = np.empty([n_test, self.Y_dim])
        with torch.no_grad():
            for i, batch in enumerate(loader):
                batch = batch.to(self.device)
                B = batch.y.shape[0]  # [this batch size]
                true_kappa[i*B: (i+1)*B, :] = (batch.y*Y_std + Y_mean).cpu().numpy()
        if save:
            np.save(path, true_kappa)
        return true_kappa

    def get_log_p_k_given_omega_int(self, n_samples, n_mc_dropout):
        path = os.path.join(self.out_dir, 'log_p_k_given_omega_int.npy')
        if os.path.exists(path):
            return np.load(path)
        k_train = self.get_true_kappa(is_train=True)
        k_bnn = self.get_bnn_kappa(n_samples=n_samples, n_mc_dropout=n_mc_dropout)
        log_p_k_given_omega_int = iutils.get_log_p_k_given_omega_int(k_train=k_train.squeeze(),
                                                                     k_bnn=k_bnn.squeeze())
        np.save(path, log_p_k_given_omega_int)
        return log_p_k_given_omega_int

    def run_mcmc_for_omega_post(self, n_samples, n_mc_dropout, mcmc_kwargs,
                                bounds_lower=-np.inf, bounds_upper=np.inf):
        k_bnn = self.get_bnn_kappa(n_samples=n_samples, n_mc_dropout=n_mc_dropout)
        log_p_k_given_omega_int = self.get_log_p_k_given_omega_int(n_samples, n_mc_dropout)
        iutils.get_omega_post(k_bnn, log_p_k_given_omega_int, mcmc_kwargs,
                              bounds_lower, bounds_upper)

    def get_kappa_log_weights(self, idx, n_samples, n_mc_dropout,
                              chain_path, chain_kwargs):
        path = os.path.join(self.out_dir, f'log_weights_{idx}.npy')
        k_bnn = self.get_bnn_kappa(n_samples=n_samples, n_mc_dropout=n_mc_dropout)
        log_p_k_given_omega_int = self.get_log_p_k_given_omega_int(n_samples, n_mc_dropout)
        omega_post_samples = iutils.get_mcmc_samples(chain_path, chain_kwargs)
        log_weights = iutils.get_kappa_log_weights(k_bnn[idx, :],
                                                   omega_post_samples,
                                                   log_p_k_given_omega_int[idx, :])
        np.save(path, log_weights)
        return log_weights

    def visualize_omega_post(self, chain_path, chain_kwargs,
                             corner_kwargs, log_idx=None):
        # MCMC samples ~ [n_omega, 2]
        omega_post_samples = iutils.get_mcmc_samples(chain_path, chain_kwargs)
        if log_idx is not None:
            omega_post_samples[:, log_idx] = np.exp(omega_post_samples[:, log_idx])
        fig = corner.corner(omega_post_samples,
                            **corner_kwargs)

        fig.savefig(os.path.join(self.out_dir, 'omega_post.pdf'))

    def visualize_kappa_post(self, idx, n_samples, n_mc_dropout,
                             chain_path, chain_kwargs):
        log_weights = self.get_kappa_log_weights(idx,
                                                 n_samples,
                                                 n_mc_dropout,
                                                 chain_path,
                                                 chain_kwargs)  # [n_samples]
        k_bnn = self.get_bnn_kappa(n_samples=n_samples,
                                   n_mc_dropout=n_mc_dropout)  # [n_test, n_samples]
        true_k = self.get_true_kappa(is_train=False)
        fig, ax = plt.subplots()
        # Original posterior
        bins = np.histogram_bin_edges(k_bnn[idx, :], bins='scott',)
        ax.hist(k_bnn[idx, :],
                histtype='step',
                bins=bins,
                density=True,
                color='#8ca252',
                label='original')
        # Reweighted posterior
        ax.hist(k_bnn[idx, :],
                histtype='step',
                bins=25,
                density=True,
                weights=np.exp(log_weights),
                color='#d6616b',
                label='reweighted')

        # Truth
        ax.axvline(true_k[idx].squeeze(), color='k', label='truth')
        ax.set_xlabel(r'$\kappa$')
        ax.legend()

    # TODO: add docstring
    # TODO: implement initialization from PSO
    # TODO: implement method `visualize_kappa_post_all` comparing before vs after
    # for all sightlines in test set
    # TODO: implement method `visualize_learned_prior` stacking predictions
    # for all sightlines in prior
    # TODO: add markdown to notebook


