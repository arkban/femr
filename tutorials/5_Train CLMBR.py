#%%

# Train CLMBR

# This tutorial walks through the various steps to train a CLMBR model.
#
# Note that CLMBR requires the gpu enabled version of FEMR. See the [README](https://github.com/som-shahlab/femr#how-to-install-femr-with-cuda-support) for the relevant instructions.
#
# Training CLMBR is a three step process:
#
# - Generating a dictionary
# - Creating batches
# - Training the model

#%%

import shutil
import os

TARGET_DIR = 'trash/tutorial_5'

os.chdir(os.path.join(os.getcwd(), "tutorials"))

if os.path.exists(TARGET_DIR):
    shutil.rmtree(TARGET_DIR)

os.mkdir(TARGET_DIR)

#%%

import os
import tempfile

EXTRACT_LOCATION = "input/extract"


"""
The first step of training CLMBR is creating a dictionary, that helps map codes to integers that can be used within a neural network.
"""

DICTIONARY_PATH = os.path.join(TARGET_DIR, "dictionary")
os.system(f"clmbr_create_dictionary {DICTIONARY_PATH} --data_path {EXTRACT_LOCATION}")

#%%

"""
The second step of training CLMBR is to prepare the batches that will actually get fed into the neural network.
"""

CLMBR_BATCHES = os.path.join(TARGET_DIR, "clmbr_batches")

os.system(
    f"clmbr_create_batches {CLMBR_BATCHES} --data_path {EXTRACT_LOCATION} --dictionary {DICTIONARY_PATH} --task clmbr --transformer_vocab_size 2048"
)


#%%

"""
Given the batches, it is now possible to train CLMBR. By default it will train for 100 epochs, with early stopping.
"""

MODEL_PATH = os.path.join(TARGET_DIR, "clmbr_model")


assert 0 == os.system(
    f"clmbr_train_model {MODEL_PATH} --data_path {EXTRACT_LOCATION} --batches_path {CLMBR_BATCHES} --learning_rate 1e-4 --rotary_type per_head --num_batch_threads 3 --max_iter 10 --n_layers 1 --hidden_size 256 --n_heads 4 --intermediate_size 256"
)

#%%