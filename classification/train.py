"""
    Contains the training and validation function and logging to Weights and Biases
    Methods:
        train: trains the model
        valid: calculates validation loss and accuracy
        set_seed: sets the random seed
        init_wandb: initializes the Weights and Biases logging


"""
import datetime
import os
from typing import Any, Tuple
import logging

import config
from dataset import get_datasets
from utils import set_seed

from torch.amp.autocast_mode import autocast
from torch.optim import Adam
from torch.utils.data import DataLoader
from tqdm import tqdm
import numpy as np
import torch
import torch.nn.functional as F
from torchmetrics.classification import MultilabelAveragePrecision
import wandb

from models.early_stopper import EarlyStopper
from models.timm_model import TimmModel

tqdm.pandas()
time_now  = datetime.datetime.now().strftime('%Y%m%d-%H%M') 
if torch.cuda.is_available():
    DEVICE = "cuda"
else:
    DEVICE = "cpu"
cfg = config.cfg
logger = logging.getLogger("acoustic_multiclass_training")

def check_shape(outputs: torch.Tensor, labels: torch.Tensor) -> None:
    """
    Checks to make sure the output is the same
    """
    if outputs.shape != labels.shape:
        logger.info(outputs.shape)
        logger.info(labels.shape)
        raise RuntimeError("Shape diff between output of models and labels, see above and debug")



# Splitting this up would be annoying!!!
# pylint: disable=too-many-statements 
# pylint: disable=too-many-locals
# pylint: disable=too-many-arguments
def train(model: Any,
        data_loader: DataLoader,
        valid_loader:  DataLoader,
        optimizer: torch.optim.Optimizer,
        scheduler,
        epoch: int,
        best_valid_map: float
       ) -> Tuple[float, float]:
    """ Trains the model
        Returns:
            loss: the average loss over the epoch
            best_valid_map: the best validation mAP
    """
    logger.debug('size of data loader: %d', len(data_loader))
    model.train()

    running_loss = 0
    log_n = 0
    log_loss = 0
    log_map = 0
    
    #scaler = torch.cuda.amp.GradScaler()
    start_time = datetime.datetime.now()
    scaler = torch.cuda.amp.grad_scaler.GradScaler()

    for i, (mels, labels) in enumerate(data_loader):
        optimizer.zero_grad()
        mels = mels.to(DEVICE)
        labels = labels.to(DEVICE)
        
        with autocast(device_type=DEVICE, dtype=torch.float16, enabled=cfg.mixed_precision):
            outputs = model(mels)
            check_shape(outputs, labels)
            loss = model.loss_fn(outputs, labels)
        outputs = outputs.to(dtype=torch.float32)
        loss = loss.to(dtype=torch.float32)

        if cfg.mixed_precision:
            # Pyright complains about scaler.scale(loss) returning iterable of unknown types
            # Problem in the pytorch typing, documentation says it returns iterables of Tensors
            #  keep if needed - noqa: reportGeneralTypeIssues 
            scaler.scale(loss).backward()  # type: ignore
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            optimizer.step()
        
        if scheduler is not None:
            scheduler.step()

        running_loss += loss.item()

        map_metric = MultilabelAveragePrecision(num_labels=model.num_classes, average="macro")
        out_for_score = outputs.detach().cpu()
        labels_for_score = labels.detach().cpu().long()
        batch_map = map_metric(out_for_score, labels_for_score).item()

        # https://forums.fast.ai/t/nan-values-when-using-precision-in-multi-classification/59767/2
        # Could be possible when model is untrained so we only have FNs
        if np.isnan(batch_map):
            batch_map = 0
        
        log_map += batch_map

        log_loss += loss.item()
        log_n += 1

        if (i != 0 and i % (cfg.logging_freq) == 0) or i == len(data_loader) - 1:
            duration = (datetime.datetime.now() - start_time).total_seconds()
            start_time = datetime.datetime.now()
            annotations = ((i % cfg.logging_freq) or cfg.logging_freq) * cfg.train_batch_size
            annotations_per_sec = annotations / duration
            epoch_progress = epoch + float(i) / len(data_loader)
            #Log to Weights and Biases
            wandb.log({
                "train/loss": log_loss / log_n,
                "train/mAP": log_map / log_n,
                "i": i,
                "epoch": epoch,
                "clips/sec": annotations_per_sec,
                "epoch_progress": epoch_progress,
            })
            logger.info("i: %s   epoch: %s   clips/s: %s   Loss: %s   mAP: %s",
                str(i).zfill(5),
                str(round(epoch_progress,3)).ljust(5, '0'),
                str(round(annotations_per_sec,3)).ljust(7), 
                str(round(log_loss / log_n,3)).ljust(5), 
                str(round(log_map / log_n,3)).ljust(5)
            )
            log_loss = 0
            log_n = 0
            log_map = 0

        if (i != 0 and i % (cfg.valid_freq) == 0):
            valid_start_time = datetime.datetime.now()
            _, _, best_valid_map = valid(model, 
                                         valid_loader, 
                                         epoch + i / len(data_loader), 
                                         best_valid_map)
            model.train()
            # Ignore the time it takes to validate in annotations/sec
            start_time += datetime.datetime.now() - valid_start_time
    return running_loss/len(data_loader), best_valid_map


# pylint: disable=too-many-locals
def valid(model: Any,
          data_loader: DataLoader,
          epoch_progress: float,
          best_valid_map: float
          ) -> Tuple[float,float, float]:
    """ Run a validation loop
    Arguments:
        model: the model to validate
        data_loader: the validation data loader
        epoch_progress: the progress of the epoch
            - Note: If this is an integer, it will run the full
                    validation set, otherwise runs cfg.valid_dataset_ratio
        best_valid_map: the best validation mAP
    Returns:
        Tuple of (loss, valid_map, best_valid_map)
    """
    model.eval()

    running_loss = 0
    pred = []
    label = []
    dataset_ratio: float = cfg.valid_dataset_ratio
    if epoch_progress.is_integer():
        dataset_ratio = 1.0

    num_valid_samples = int(len(data_loader)*dataset_ratio)

    # tqdm is a progress bar
    dl_iter = tqdm(data_loader, position=5, total=num_valid_samples)

    if cfg.map_debug and cfg.model_checkpoint is not None:
        pred = torch.load("/".join(cfg.model_checkpoint.split('/')[:-1]) + '/pred.pt')
        label = torch.load("/".join(cfg.model_checkpoint.split('/')[:-1]) + '/label.pt')
    else:
        with torch.no_grad():
            for index, (mels, labels) in enumerate(dl_iter):
                if index > len(dl_iter) * dataset_ratio:
                    # Stop early if not doing full validation
                    break
                mels = mels.to(DEVICE)
                labels = labels.to(DEVICE)
                
                # argmax
                outputs = model(mels)
                check_shape(outputs, labels)
                
                loss = model.loss_fn(outputs, labels)
                    
                running_loss += loss.item()
                
                pred.append(outputs.cpu().detach())
                label.append(labels.cpu().detach())


            pred = torch.cat(pred)
            label = torch.cat(label)
            if cfg.map_debug and cfg.model_checkpoint is not None:
                torch.save(pred, "/".join(cfg.model_checkpoint.split('/')[:-1]) + '/pred.pt')
                torch.save(label, "/".join(cfg.model_checkpoint.split('/')[:-1]) + '/label.pt')

    # softmax predictions
    pred = F.sigmoid(pred).to(DEVICE)

    #metric = MultilabelAveragePrecision(num_labels=model.num_classes, average="macro")
    #valid_map = metric(pred.detach().cpu(), label.detach().cpu().long())

    map_metric = MultilabelAveragePrecision(num_labels=model.num_classes, average="macro")
    out_for_score = pred.detach().cpu()
    labels_for_score = label.detach().cpu().long()
    valid_map = map_metric(out_for_score, labels_for_score).item()

    # Log to Weights and Biases
    wandb.log({
        "valid/loss": running_loss/num_valid_samples,
        "valid/map": valid_map,
        "epoch_progress": epoch_progress,
    })

    logger.info("Validation Loss:\t%f\nValidation mAP:\t%f", 
                running_loss/len(data_loader),
                valid_map)

    if valid_map > best_valid_map:
        path = os.path.join("models", f"{cfg.model}-{time_now}.pt")
        if not os.path.exists("models"):
            os.mkdir("models")
        torch.save(model.state_dict(), path)
        logger.info("Model saved in: %s", path)
        logger.info("Validation mAP Improved - %f ---> %f", best_valid_map, valid_map)
        best_valid_map = valid_map

    
    return running_loss/len(data_loader), valid_map, best_valid_map


def init_wandb() -> Any:
    """
    Initialize the weights and biases logging
    """
    run = wandb.init(
        entity=cfg.wandb_entity,
        project=cfg.wandb_project,
        config=cfg.config_dict,
        mode="online" if cfg.logging else "disabled"
    )

    assert run is not None
    if cfg.wandb_run_name == "auto":
        # This variable is always defined
        cfg.wandb_run_name = cfg.model # type: ignore
    run.name = f"{cfg.wandb_run_name}-{time_now}"

    return run

def load_datasets(train_dataset, val_dataset
        )-> Tuple[DataLoader, DataLoader]:
    """
        Loads datasets and dataloaders for train and validation
    """

    train_dataloader = DataLoader(
        train_dataset,
        cfg.train_batch_size,
        shuffle=True,
        num_workers=cfg.jobs,
    )
    val_dataloader = DataLoader(
        val_dataset,
        cfg.validation_batch_size,
        shuffle=False,
        num_workers=cfg.jobs,
    )
    return train_dataloader, val_dataloader

def logging_setup() -> None:
    """ Setup logging on the main process
    Display config information
    """
    file_handler = logging.FileHandler("recent.log", mode='w')
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.debug("Debug logging enabled")
    logger.debug("Config: %s", cfg.config_dict)
    logger.debug("Git hash: %s", cfg.git_hash)

def main() -> None:
    """ Main function
    """
    torch.multiprocessing.set_start_method('spawn')
    logger.info("Device is: %s",DEVICE)
    init_wandb()
    logging_setup()
    assert wandb.run is not None
    set_seed(cfg.seed)

    # Load in dataset
    logger.info("Loading Dataset")
    # pylint: disable=unused-variable
    train_dataset, val_dataset = get_datasets()
    train_dataloader, val_dataloader = load_datasets(train_dataset, val_dataset)

    logger.info("Loading Model...")
    model_for_run = TimmModel(num_classes=train_dataset.num_classes, 
                              model_name=cfg.model).to(DEVICE)
    model_for_run.create_loss_fn(train_dataset)
    if cfg.model_checkpoint != "":
        model_for_run.load_state_dict(torch.load(cfg.model_checkpoint))
    optimizer = Adam(model_for_run.parameters(), lr=cfg.learning_rate)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, eta_min=1e-5, T_max=10)
    logger.info("Model / Optimizer Loading Successful :P")
    
    logger.info("Training")
    best_valid_map = 0
    early_stopper = EarlyStopper(patience=cfg.patience, min_delta=cfg.min_valid_map_delta)
    for epoch in range(cfg.epochs):
        logger.info("Epoch %d", epoch)

        _, best_valid_map = train(
            model_for_run,
            train_dataloader,
            val_dataloader,
            optimizer,
            scheduler,
            epoch,
            best_valid_map
        )
        _, valid_map, best_valid_map = valid(model_for_run, 
                                             val_dataloader, 
                                             epoch + 1.0, 
                                             best_valid_map)

        logger.info("Best validation map: %f", best_valid_map)
        if cfg.early_stopping and early_stopper.early_stop(valid_map):
            logger.info("Early stopping has triggered on epoch %d", epoch)
            break

        
if __name__ == '__main__':
    main()
