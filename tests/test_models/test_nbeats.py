import shutil
import pytorch_lightning as pl
from pytorch_lightning.loggers import TensorBoardLogger
from pytorch_lightning.callbacks import EarlyStopping
from pytorch_forecasting.metrics import QuantileLoss
from pytorch_forecasting.models import NBeats


def test_integration(dataloaders_fixed_window_without_coveratiates, tmp_path):
    train_dataloader = dataloaders_fixed_window_without_coveratiates["train"]
    val_dataloader = dataloaders_fixed_window_without_coveratiates["val"]
    early_stop_callback = EarlyStopping(monitor="val_loss", min_delta=1e-4, patience=1, verbose=False, mode="min")

    logger = TensorBoardLogger(tmp_path)
    trainer = pl.Trainer(
        max_epochs=3,
        gpus=0,
        weights_summary="top",
        gradient_clip_val=0.1,
        early_stop_callback=early_stop_callback,
        fast_dev_run=True,
        logger=logger,
    )

    net = NBeats.from_dataset(train_dataloader.dataset, learning_rate=0.15, log_gradient_flow=True, widths=[4, 4, 4])
    net.size()
    try:
        trainer.fit(
            net, train_dataloader=train_dataloader, val_dataloaders=val_dataloader,
        )
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)

    net.predict(val_dataloader, fast_dev_run=True, return_index=True, return_decoder_lengths=True)
