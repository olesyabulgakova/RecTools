#  Copyright 2025 MTS (Mobile Telesystems)
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""DSSM model."""


# pylint: disable=abstract-method
from __future__ import annotations

import typing as tp
import warnings
from copy import deepcopy

import numpy as np
import torch
import torch.nn.functional as F

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from pytorch_lightning import Callback, LightningModule, Trainer
    from pytorch_lightning.loggers import Logger

from torch import nn
from torch.utils.data import DataLoader

from rectools.dataset import Dataset
from rectools.dataset.torch_datasets import (
    DSSMItemDataset,
    DSSMItemDatasetBase,
    DSSMTrainDataset,
    DSSMTrainDatasetBase,
    DSSMUserDataset,
    DSSMUserDatasetBase,
)
from rectools.exceptions import NotFittedError
from rectools.types import InternalIdsArray

from ..base import InternalRecoTriplet
from ..rank import Distance
from ..vector import Factors, VectorModel


class ItemNet(nn.Module):
    def __init__(
        self,
        n_factors: int,
        dim_input: int,
        activation: tp.Callable[[torch.Tensor], torch.Tensor] = F.elu,
    ) -> None:
        super().__init__()
        self.embedding_layer = nn.Linear(dim_input, n_factors, bias=False)
        self.dense_layer = nn.Linear(n_factors, n_factors, bias=False)
        self.output_layer = nn.Linear(n_factors, n_factors, bias=False)
        self.activation = activation

    def forward(self, item_features: torch.Tensor) -> torch.Tensor:
        emb = self.activation(self.embedding_layer(item_features))
        features = self.activation(self.dense_layer(emb))
        x = emb + features

        output = self.output_layer(x)
        return output


class UserNet(nn.Module):
    def __init__(
        self,
        n_factors: int,
        dim_input: int,
        dim_interactions: int,
        activation: tp.Callable[[torch.Tensor], torch.Tensor] = F.elu,
    ) -> None:
        super().__init__()
        self.embedding_interactions_layer = nn.Linear(dim_interactions, n_factors, bias=False)
        self.embedding_features_layer = nn.Linear(dim_input, n_factors, bias=False)

        self.features_dense_layer = nn.Linear(n_factors, n_factors, bias=False)
        self.output_layer = nn.Linear(n_factors * 2, n_factors, bias=False)
        self.activation = activation

    def forward(self, user_features: torch.Tensor, interactions: torch.Tensor) -> torch.Tensor:
        features_emb = self.activation(self.embedding_features_layer(user_features))
        interactions_emb = self.activation(self.embedding_interactions_layer(interactions))
        features_dense = self.activation(self.features_dense_layer(features_emb))
        features_x = features_emb + features_dense
        concatenated_features = torch.cat((features_x, interactions_emb), 1)

        output = self.output_layer(concatenated_features)
        return output


class DSSM(LightningModule):
    """
    DSSM module for item to item or user to item recommendations.
    This implementation uses triplet loss (see https://en.wikipedia.org/wiki/Triplet_loss)
    as it's objective function. As an input it expects one-hot encoded item features,
    one-hot encoded user features and one-hot encoded interactions. Those as easily extracted
    via `rectools.dataset.Dataset`.
    During the training cycle item features are propagated through fully connected
    item network, user features and interactions are propagated through fully connected
    user network.

    Parameters
    ----------
    n_factors_user : int
        How many hidden units to use in user network.
    n_factors_item : int
        How many hidden units to use in item network.
    dim_input_user : int
        User features dimensionality.
    dim_input_item : int
        Item features dimensionality.
    dim_interactions : int
        Interactions dimensionality.
    activation : Callable, default `torch.nn.functional.elu`
        Which activation function to use. This function must take a tensor and return a tensor
    lr : float, default 0.01
        Learning rate.
    triplet_loss_margin : float, default 0.4
        A nonnegative margin representing the minimum difference between
        the positive and negative distances required for the loss to be 0.
        Larger margins penalize cases where the negative examples are not
        distant enough from the anchors, relative to the positives.
    weight_decay : float, default 1e-6
        weight decay (L2 penalty).
    log_to_prog_bar : bool, default True
        Whether to enable logging train and validation losses to progress bar.
    """

    def __init__(
        self,
        n_factors_user: int,
        n_factors_item: int,
        dim_input_user: int,
        dim_input_item: int,
        dim_interactions: int,
        activation: tp.Callable[[torch.Tensor], torch.Tensor] = F.elu,
        lr: float = 0.01,
        triplet_loss_margin: float = 0.4,
        weight_decay: float = 1e-6,
        log_to_prog_bar: bool = True,
    ) -> None:
        super().__init__()
        self.user_net = UserNet(n_factors_user, dim_input_user, dim_interactions, activation)
        self.item_net = ItemNet(n_factors_item, dim_input_item, activation)
        self.lr = lr
        self.triplet_loss_margin = triplet_loss_margin
        self.weight_decay = weight_decay
        self.log_to_prog_bar = log_to_prog_bar

    def forward(
        self,
        item_features_pos: torch.Tensor,
        item_features_neg: torch.Tensor,
        user_features: torch.Tensor,
        interactions: torch.Tensor,
    ) -> tp.Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        anchor = self.user_net(user_features, interactions)
        pos = self.item_net(item_features_pos)
        neg = self.item_net(item_features_neg)

        return anchor, pos, neg

    def configure_optimizers(self) -> torch.optim.Adam:
        """Choose what optimizers and learning-rate schedulers to use in optimization"""
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        return optimizer

    def training_step(self, batch: tp.Sequence[torch.Tensor], batch_idx: int) -> torch.Tensor:
        """Compute and return the training loss"""
        user_features, interactions, pos, neg = batch
        anchor, positive, negative = self(pos, neg, user_features, interactions)
        loss = F.triplet_margin_loss(anchor, positive, negative, margin=self.triplet_loss_margin)
        self.log("loss", loss.item(), prog_bar=self.log_to_prog_bar)
        return loss

    def validation_step(self, batch: tp.Sequence[torch.Tensor], batch_idx: int) -> torch.Tensor:
        user_features, interactions, pos, neg = batch
        anchor, positive, negative = self(pos, neg, user_features, interactions)
        val_loss = F.triplet_margin_loss(anchor, positive, negative, margin=self.triplet_loss_margin)
        self.log("val_loss", val_loss.item(), prog_bar=self.log_to_prog_bar)
        return val_loss

    def inference_items(self, dataloader: DataLoader[tp.Any]) -> np.ndarray:
        batches = []
        self.eval()
        for batch in dataloader:
            item_features = batch
            with torch.no_grad():
                v_batch = self.item_net(item_features.to(self.device))
            batches.append(v_batch)
        vectors = torch.cat(batches, dim=0).cpu().numpy()
        return vectors

    def inference_users(self, dataloader: DataLoader[tp.Any]) -> np.ndarray:
        batches = []
        self.eval()
        for batch in dataloader:
            user_features, interactions = batch
            with torch.no_grad():
                v_batch = self.user_net(user_features.to(self.device), interactions.to(self.device))
            batches.append(v_batch)
        vectors = torch.cat(batches, dim=0).cpu().numpy()
        return vectors


class DSSMModel(VectorModel):  # pylint: disable=too-many-instance-attributes
    """
    Wrapper for `rectools.models.dssm.DSSM`

    Parameters
    ----------
    train_dataset_type : Type(DSSMTrainDatasetBase), default `DSSMTrainDataset`
        Type of dataset used for training.
        A child of `torch.utils.data.Dataset` that implements `from_dataset` classmethod.
        Used to construct `torch.utils.data.Dataset` from a given `rectools.dataset.dataset.Dataset`.
    user_dataset_type : Type(DSSMUserDatasetBase), default `DSSMUserDataset`
        Type of dataset used for user inference.
        A child of `torch.utils.data.Dataset` that implements `from_dataset` classmethod.
        Used to construct `torch.utils.data.Dataset` from a given `rectools.dataset.dataset.Dataset`.
    item_dataset_type : Type(DSSMItemDatasetBase), default `DSSMItemDataset`
        Type of dataset used for item inference.
        A child of `torch.utils.data.Dataset` that implements `from_dataset` classmethod.
        Used to construct `torch.utils.data.Dataset` from a given `rectools.dataset.dataset.Dataset`.
    model : Optional(DSSM), default None
        Which model to wrap.
        If model is None, an instance of default DSSM is created during fit.
    n_factors: int, default 128
        How many hidden units to use in user and item networks.
        Used only if `model` is None.
    max_epochs : int, default 5
        Stop training if this number of epochs is reached.
        Keep in mind that if any kind of early stopping callback is passed
        as one of the callbacks along with a validation dataset,
        then hitting exactly max_epochs is not guaranteed.
    batch_size : int, default 128
        How many samples per batch to load.
    dataloader_num_workers : int, default 0
        How many processes to use for data loading. Defaults to 0, which means that
        all data will be loaded in the main process.
    trainer_sanity_steps : int, default 2
        Sanity check runs n validation batches before starting the training routine.
    trainer_devices : str | int, default 1
        "auto" means determine the number of available devices based on the `trainer_accelerator` type.
        In case on an integer, it will be mapped to either `gpus`, `tpu_cores`, `num_processes` or `ipus`,
        based on the accelerator type.
    trainer_accelerator : str, default 'auto'
        Supports passing different accelerator types ("cpu", "gpu", "tpu", "ipu", "auto").
        The "auto" option recognizes the machine you are on, and selects the respective.
    callbacks : Optional(Sequence(Callback)), default None
        Which callbacks to use. For instance, `pytorch_lightning.callbacks.TQDMProgressBar`, etc.
    loggers : LightningLoggerBase | iterable(LightningLoggerBase) | bool, default True
        Which loggers to use. For instance, `pytorch_lightning.loggers.TensorboardLogger`, etc.
    verbose : int, default 0
        Verbosity level (applies only to recommend loop).
    deterministic : bool, default ``False``
        If ``True``, sets whether PyTorch operations must use deterministic algorithms.
        Use `pytorch_lightning.seed_everything` together with this param to fix the random state.
    recommend_n_threads: int, default 0
        Number of threads to use for recommendation ranking on CPU.
        Specifying ``0`` means to default to the number of cores on the machine.
        If you want to change this parameter after model is initialized,
        you can manually assign new value to model `recommend_n_threads` attribute.
    recommend_use_gpu_ranking: bool, default ``True``
        Flag to use GPU for recommendation ranking. Please note that GPU and CPU ranking may provide
        different ordering of items with identical scores in recommendation table.
        If ``True``, `implicit.gpu.HAS_CUDA` will also be checked before ranking.
        If you want to change this parameter after model is initialized,
        you can manually assign new value to model `recommend_use_gpu_ranking` attribute.
    """

    recommends_for_warm = True
    recommends_for_cold = False

    u2i_dist = Distance.EUCLIDEAN
    i2i_dist = Distance.EUCLIDEAN

    def __init__(
        self,
        train_dataset_type: tp.Type[DSSMTrainDatasetBase] = DSSMTrainDataset,
        user_dataset_type: tp.Type[DSSMUserDatasetBase] = DSSMUserDataset,
        item_dataset_type: tp.Type[DSSMItemDatasetBase] = DSSMItemDataset,
        model: tp.Optional[DSSM] = None,
        n_factors: int = 128,
        max_epochs: int = 5,
        batch_size: int = 128,
        dataloader_num_workers: int = 0,
        trainer_sanity_steps: int = 2,
        trainer_devices: tp.Union[str, int] = 1,
        trainer_accelerator: str = "auto",
        callbacks: tp.Optional[tp.Union[tp.List[Callback], Callback]] = None,
        loggers: tp.Union[Logger, tp.Iterable[Logger], bool] = True,
        verbose: int = 0,
        deterministic: bool = False,
        recommend_n_threads: int = 0,
        recommend_use_gpu_ranking: bool = True,
    ) -> None:
        super().__init__(verbose=verbose)
        self.model: DSSM
        self._model = model
        self.n_factors = n_factors
        self.max_epochs = max_epochs
        self.batch_size = batch_size
        self.trainer: Trainer
        self._trainer = Trainer(
            devices=trainer_devices,
            accelerator=trainer_accelerator,
            max_epochs=self.max_epochs,
            num_sanity_val_steps=trainer_sanity_steps,
            callbacks=callbacks,
            logger=loggers,
            deterministic=deterministic,
        )
        self.dataloader_num_workers = dataloader_num_workers
        self.train_dataset_type = train_dataset_type
        self.user_dataset_type = user_dataset_type
        self.item_dataset_type = item_dataset_type
        self.recommend_n_threads = recommend_n_threads
        self.recommend_use_gpu_ranking = recommend_use_gpu_ranking

    def _fit(self, dataset: Dataset, dataset_valid: tp.Optional[Dataset] = None) -> None:  # type: ignore
        self.trainer = deepcopy(self._trainer)

        if self._model is None:
            if dataset.user_features is None or dataset.item_features is None:
                raise ValueError("DSSM model requires user and item features to be present in the dataset.")
            self.model = DSSM(
                n_factors_user=self.n_factors,
                n_factors_item=self.n_factors,
                dim_input_user=dataset.user_features.get_sparse().shape[1],
                dim_input_item=dataset.item_features.get_sparse().shape[1],
                dim_interactions=dataset.get_user_item_matrix().shape[1],
            )
        else:
            self.model = deepcopy(self._model)

        train_dataset = self.train_dataset_type.from_dataset(dataset)
        train_dataloader = DataLoader(
            train_dataset,
            batch_size=self.batch_size,
            num_workers=self.dataloader_num_workers,
            shuffle=True,
        )
        valid_dataloader = None
        if dataset_valid is not None:
            valid_dataset = self.train_dataset_type.from_dataset(dataset_valid)
            valid_dataloader = DataLoader(
                valid_dataset,
                batch_size=self.batch_size,
                num_workers=self.dataloader_num_workers,
                shuffle=False,
            )

        self.trainer.fit(
            model=self.model,
            train_dataloaders=train_dataloader,
            val_dataloaders=valid_dataloader,
        )

    def get_vectors(self, dataset: Dataset) -> tp.Tuple[np.ndarray, np.ndarray]:
        if not self.is_fitted:
            raise NotFittedError(self.__class__.__name__)
        user_factors = self._get_users_factors(dataset)
        item_factors = self._get_items_factors(dataset)
        return user_factors.embeddings, item_factors.embeddings

    def _get_users_factors(self, dataset: Dataset) -> Factors:
        dataloader = DataLoader(
            self.user_dataset_type.from_dataset(dataset),
            batch_size=self.batch_size,
            num_workers=self.dataloader_num_workers,
            shuffle=False,
        )
        vectors = self.model.inference_users(dataloader)
        return Factors(vectors)

    def _get_items_factors(self, dataset: Dataset) -> Factors:
        dataloader = DataLoader(
            self.item_dataset_type.from_dataset(dataset),
            batch_size=self.batch_size,
            num_workers=self.dataloader_num_workers,
            shuffle=False,
        )
        vectors = self.model.inference_items(dataloader)
        return Factors(vectors)

    def _recommend_u2i_warm(
        self,
        user_ids: InternalIdsArray,
        dataset: Dataset,
        k: int,
        sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray],
    ) -> InternalRecoTriplet:
        return self._recommend_u2i(user_ids, dataset, k, False, sorted_item_ids_to_recommend)

    def _recommend_i2i_warm(
        self,
        target_ids: InternalIdsArray,
        dataset: Dataset,
        k: int,
        sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray],
    ) -> InternalRecoTriplet:
        return self._recommend_i2i(target_ids, dataset, k, sorted_item_ids_to_recommend)
