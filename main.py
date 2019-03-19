from os import path
from typing import Iterator, List, Dict
import sys

import numpy as np
import torch
import torch.optim as optim
from torch.utils.data import random_split
from torch.utils.data.dataset import Subset

from allennlp.common.file_utils import cached_path
from allennlp.data import Instance
from allennlp.data.dataset_readers import DatasetReader
from allennlp.data.fields import ArrayField, MetadataField, TextField, SequenceLabelField
from allennlp.data.iterators import BucketIterator
from allennlp.data.token_indexers import TokenIndexer, SingleIdTokenIndexer
from allennlp.data.tokenizers import Token
from allennlp.data.vocabulary import Vocabulary
from allennlp.models import Model
from allennlp.modules.elmo import Elmo, ELMoTokenCharactersIndexer
from allennlp.modules.text_field_embedders import TextFieldEmbedder, BasicTextFieldEmbedder
from allennlp.modules.token_embedders import Embedding
from allennlp.modules.seq2vec_encoders.pytorch_seq2vec_wrapper import Seq2VecEncoder, PytorchSeq2VecWrapper
from allennlp.nn.util import get_text_field_mask, sequence_cross_entropy_with_logits
from allennlp.training.metrics import Covariance, CategoricalAccuracy, PearsonCorrelation
from allennlp.training.trainer import Trainer
from allennlp.predictors import SentenceTaggerPredictor

from embedders import ELMoTextFieldEmbedder
from kfold import StratifiedKFold

torch.manual_seed(1)

class WmtDatasetReader(DatasetReader):
    def __init__(self, token_indexers: Dict[str, TokenIndexer] = None) -> None:
        super().__init__(lazy=False)
        self.token_indexers = token_indexers or {"tokens": ELMoTokenCharactersIndexer()}

    def text_to_instance(self,
                         mt_tokens: List[Token],
                         ref_tokens: List[Token],
                         human_score: float,
                         origin: str) -> Instance:
        mt_sent = TextField(mt_tokens, self.token_indexers)
        ref_sent = TextField(ref_tokens, self.token_indexers)
        human_score = ArrayField(np.array([human_score]))
        origin = MetadataField(origin)

        return Instance({"mt_sent": mt_sent,
                         "ref_sent": ref_sent,
                         "human_score": human_score,
                         "origin": origin})

    def _read(self, file_path: str) -> Iterator[Instance]:
        with open(file_path, mode='r', encoding='utf-8') as file:
            for line in file:
                mt_text, ref_text, score_text, origin = line.strip().split('\t')
                mt_words, ref_words, human_score = mt_text.split(), ref_text.split(), float(score_text)
                yield self.text_to_instance(
                        [Token(word) for word in mt_words],
                        [Token(word) for word in ref_words],
                        human_score,
                        origin)

class RuseModel(Model):
    def __init__(self,
                 word_embeddings: TextFieldEmbedder,
                 encoder: Seq2VecEncoder,
                 vocab: Vocabulary) -> None:
        super().__init__(vocab)
        self.word_embeddings = word_embeddings
        self.encoder = encoder

        hidden_dim = 128
        self.mlp = torch.nn.Sequential(
                torch.nn.Linear(in_features=encoder.get_output_dim()*4, out_features=hidden_dim),
                torch.nn.Tanh(),
                torch.nn.Linear(in_features=hidden_dim, out_features=hidden_dim),
                torch.nn.Tanh(),
                torch.nn.Linear(in_features=hidden_dim, out_features=1)
            )
        self.covar = Covariance()
        self.pearson = PearsonCorrelation()

    def forward(self,
                mt_sent: Dict[str, torch.Tensor],
                ref_sent: Dict[str, torch.Tensor],
                human_score: np.ndarray,
                origin: str) -> Dict[str, torch.Tensor]:
        mt_mask = get_text_field_mask(mt_sent)
        ref_mask = get_text_field_mask(ref_sent)

        mt_embeddings = self.word_embeddings(mt_sent)
        ref_embeddings = self.word_embeddings(ref_sent)

        mt_encoder_out = self.encoder(mt_embeddings, mt_mask)
        ref_encoder_out = self.encoder(ref_embeddings, ref_mask)
    
        input = torch.cat((mt_encoder_out,
                           ref_encoder_out,
                           torch.mul(mt_encoder_out, ref_encoder_out),
                           torch.abs(mt_encoder_out - ref_encoder_out)), 1)
        reg = self.mlp(input)
        output = {"reg": reg}

        if human_score is not None:
            # run metric calculation
            self.covar(reg, human_score)
            self.pearson(reg, human_score)

            # calculate mean squared error
            delta = reg - human_score
            output["loss"] = torch.mul(delta, delta).sum()

        return output

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        return {"covar": self.covar.get_metric(reset),
                "pearson": self.pearson.get_metric(reset)}

def origin_of(instance):
    return instance.fields["origin"].metadata

THIS_DIR = path.dirname(path.realpath(__file__))
DATA_DIR = path.join(THIS_DIR, 'data', 'trg-en')
DATASET_PATH = path.join(DATA_DIR, 'combined')
OPTIONS_FILE = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/2x4096_512_2048cnn_2xhighway/elmo_2x4096_512_2048cnn_2xhighway_options.json"
WEIGHTS_FILE = "https://s3-us-west-2.amazonaws.com/allennlp/models/elmo/2x4096_512_2048cnn_2xhighway/elmo_2x4096_512_2048cnn_2xhighway_weights.hdf5"

reader = WmtDatasetReader()
dataset = reader.read(cached_path(DATASET_PATH))

# TODO: We should be implementing 10-fold cross validation.
# We split each dataset into 10 pieces. This gives us 30 segments.
# We combine the segments in triples, giving us 10 segments equally composed of 2015/16/17 data. (aka **stratified** k fold CV)
# 10 times, we train on 9 of the segments and validate on 1 of them.
# We get a validation loss each time, and the average of these is the "cross-validation loss".
# We choose the set of hyperparameters (via grid search) that minimizes the cross-validation loss.
grid = {
    "batch_size": [64, 128, 256, 512, 1024]
}
grid_iter = GridIterator(grid)

# TODO: We should cache the results so we don't have to train again with these parameters
best_params = min(grid_iter, key=__)
print(best_params)

def __(params):
    vocab = Vocabulary.from_instances(dataset)
    # TODO: Figure out the best parameters here
    elmo = Elmo(cached_path(OPTIONS_FILE),
                cached_path(WEIGHTS_FILE),
                num_output_representations=2,
                dropout=0)
    word_embeddings = ELMoTextFieldEmbedder({"tokens": elmo})
    # TODO: Figure out the best parameters here
    lstm = PytorchSeq2VecWrapper(torch.nn.LSTM(input_size=elmo.get_output_dim(),
                                               hidden_size=64,
                                               num_layers=2,
                                               batch_first=True))

    model = RuseModel(word_embeddings, lstm, vocab)
    optimizer = optim.Adam(model.parameters())
    # TODO: What kind of iterator should be used?
    iterator = BucketIterator(batch_size=params["batch_size"],
                              sorting_keys=[("mt_sent", "num_tokens"),
                                            ("ref_sent", "num_tokens")])
    iterator.index_with(vocab)

    # Calculate the validation loss for each train-test split
    losses = []
    kfold = StratifiedKFold(dataset, k=10, grouping=origin_of)
    for train, val in kfold:
        # TODO: Figure out best hyperparameters
        trainer = Trainer(model=model,
                          optimizer=optimizer,
                          iterator=iterator,
                          cuda_device=0,
                          train_dataset=train,
                          validation_dataset=val,
                          patience=10,
                          num_epochs=1000)
        trainer.train()

        # TODO: Better way to access the validation loss?
        loss, _ = trainer._validation_loss()
        losses.append(loss)
    average_loss = np.mean(losses)
    return average_loss
