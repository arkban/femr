import argparse
import datetime
import functools
import json
import logging
import os
import pickle
import random
import tempfile
from typing import TypeVar

import haiku as hk
import jax
import jax.numpy as jnp
import jmp
import msgpack
import numpy as np
import optax
import sklearn.metrics

import femr.datasets
import femr.extension.dataloader
import femr.models.dataloader
import femr.models.transformer

T = TypeVar("T")


def create_dictionary() -> None:
    parser = argparse.ArgumentParser(prog="Create dictionary")
    parser.add_argument("output_file", type=str)
    parser.add_argument("--data_path", type=str, required=True)

    args = parser.parse_args()

    femr.extension.dataloader.create_dictionary(args.data_path, args.output_file)


def create_survival_dictionary() -> None:
    parser = argparse.ArgumentParser(prog="Create survival dictionary")
    parser.add_argument("output_file", type=str)
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--num_buckets", type=int, required=True)
    parser.add_argument("--size", type=int, required=True)

    args = parser.parse_args()

    femr.extension.dataloader.create_survival_dictionary(args.data_path, args.output_file, args.num_buckets, args.size)


def train_model() -> None:
    os.environ["JAX_NUMPY_RANK_PROMOTION"] = "raise"
    os.environ["XLA_PYTHON_CLIENT_MEM_FRACTION"] = "0.98"

    parser = argparse.ArgumentParser(prog="Train")
    parser.add_argument("directory", type=str)
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--batches_path", type=str, required=True)
    parser.add_argument("--learning_rate", type=float, required=True)
    parser.add_argument("--rotary_type", type=str, required=True)
    parser.add_argument("--clmbr_survival_dim", type=int)
    parser.add_argument("--num_batch_threads", type=int)
    parser.add_argument("--start_from_checkpoint", type=str)
    parser.add_argument("--freeze_weights", default=False, action="store_true")
    parser.add_argument("--token_dropout", type=float, default=0)
    parser.add_argument("--internal_dropout", type=float, default=0)
    parser.add_argument("--weight_decay", type=float, default=0)
    parser.add_argument("--max_iter", type=int, default=None)
    parser.add_argument("--hidden_size", type=int, default=768, help="Transformer hidden size")
    parser.add_argument("--intermediate_size", type=int, default=3072, help="Transformer intermediate layer size")
    parser.add_argument("--n_heads", type=int, default=12, help="Transformer # of heads")
    parser.add_argument("--n_layers", type=int, default=6, help="Transformer # of layers")
    parser.add_argument("--attention_width", type=int, default=512, help="Transformer attention width.")
    parser.add_argument(
        "--dev_batches_path",
        type=str,
        required=False,
        help="Do early stopping with a different set of batches instead of the development set",
    )
    parser.add_argument("--linear_probe", type=str, required=False)

    parser.add_argument(
        "--early_stopping_window_steps",
        type=int,
        default=None,
        help="If we don't see a decrease in dev loss in this many steps, stop training. A reasonable value is 15000.",
    )
    parser.add_argument("--with_age_beta", default=False, action="store_true")

    args = parser.parse_args()

    os.mkdir(args.directory)

    logFormatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
    rootLogger = logging.getLogger()

    fileHandler = logging.FileHandler(os.path.join(args.directory, "log"))
    fileHandler.setFormatter(logFormatter)

    ch = logging.StreamHandler()
    ch.setFormatter(logFormatter)

    rootLogger.addHandler(fileHandler)
    rootLogger.addHandler(ch)

    rootLogger.setLevel(logging.INFO)
    rootLogger.info(f"Training model with {args}")

    batch_info_path = os.path.join(args.batches_path, "batch_info.msgpack")

    with open(batch_info_path, "rb") as f:
        batch_info = msgpack.load(f, use_list=False)

    batch_config = batch_info["config"]

    del batch_info

    batch_task = batch_config["task"]
    task = {}
    task["type"] = batch_task["type"]
    if task["type"] == "survival_clmbr":
        task["num_time_bins"] = len(batch_task["survival_dict"]["time_bins"])
        task["num_codes"] = len(batch_task["survival_dict"]["codes"])
        task["dim"] = args.clmbr_survival_dim
        task["time_bins"] = batch_task["survival_dict"]["time_bins"]

    elif task["type"] == "clmbr":
        task["vocab_size"] = batch_task["vocab_size"]
    elif task["type"] == "labeled_patients":
        task["labeler_type"] = batch_task["labeler_type"]
        if task["labeler_type"] == "survival":
            assert args.start_from_checkpoint is not None

            with open(os.path.join(args.start_from_checkpoint, "config.msgpack"), "rb") as f:
                config = msgpack.load(f)
                assert config["task"] == "survival_clmbr"

                task["time_bins"] = config["task"]["time_bins"]
                task["dim"] = config["task"]["dim"]

                del config
    else:
        rootLogger.error("Invalid task? " + batch_task["task"])
        exit()

    config = {
        "data_path": args.data_path,
        "batch_info_path": batch_info_path,
        "seed": batch_config["seed"],
        "task": task,
        "transformer": {
            "vocab_size": batch_config["transformer"]["vocab_size"],
            "hidden_size": args.hidden_size,
            "intermediate_size": args.intermediate_size,
            "n_heads": args.n_heads,
            "n_layers": args.n_layers,
            "rotary": args.rotary_type,
            "attention_width": args.attention_width - 16,  # 16 is the width of the tiling
            "internal_dropout": args.internal_dropout,
            "is_hierarchical": batch_config["transformer"]["is_hierarchical"],
            "note_embedding_data": batch_config["transformer"].get("note_embedding_data"),
            "with_age_beta": args.with_age_beta,
        },
        "learning_rate": args.learning_rate,
        "max_grad_norm": 1.0,
        "weight_decay": args.weight_decay,
        "n_epochs": 100,
    }
    del batch_config

    logging.info("Got config %s", config)

    random.seed(config["seed"])

    config_path = os.path.join(args.directory, "config.msgpack")
    with open(config_path, "wb") as out:
        msgpack.dump(config, out)

    config = hk.data_structures.to_immutable_dict(config)

    if args.dev_batches_path:
        dev_batch_info_path = os.path.join(args.dev_batches_path, "batch_info.msgpack")
        dev_loader = femr.models.dataloader.BatchLoader(args.data_path, dev_batch_info_path)

    loader = femr.models.dataloader.BatchLoader(args.data_path, batch_info_path)

    logging.info(
        "Loaded batches %s %s",
        loader.get_number_of_batches("train"),
        loader.get_number_of_batches("dev"),
    )

    def model_fn(config, batch, is_training):
        model = femr.models.transformer.EHRTransformer(config)(batch, is_training)
        return model

    dummy_batch = loader.get_batch("train", 0)
    dummy_batch = jax.tree_map(lambda a: jnp.array(a), dummy_batch)

    logging.info(
        "Got dummy batch %s",
        str(jax.tree_map(lambda a: (a.shape, a.dtype, a.device()), dummy_batch)),
    )

    rng = jax.random.PRNGKey(42)
    model = hk.transform(model_fn)

    logging.info("Transformed the model function")

    params = jax.jit(model.init, static_argnames=["config", "is_training"])(
        rng, config=config, batch=dummy_batch, is_training=True
    )

    def replace(params, module, weight, value):
        old_val = params[module][weight]
        params[module][weight] = value.astype(old_val.dtype).reshape(old_val.shape)

    if task["type"] == "survival_clmbr":
        replace(
            params,
            "EHRTransformer/~/SurvivalCLMBRTask",
            "code_weight_bias",
            jnp.log2(jnp.array(batch_task["survival_dict"]["lambdas"])),
        )
    elif task["type"] == "clmbr":
        pass
    elif task["type"] == "labeled_patients":
        if args.linear_probe is not None:
            with open(args.linear_probe, "rb") as f:
                linear_probe = pickle.load(f)

            if task["labeler_type"] == "survival":
                assert False
            elif task["labeler_type"] == "boolean":
                print(linear_probe.shape)
                # print(params["EHRTransformer/~/BooleanClassifier/~/linear"])
                replace(params, "EHRTransformer/~/BooleanClassifier/~/linear", "b", linear_probe[-1:])
                replace(params, "EHRTransformer/~/BooleanClassifier/~/linear", "w", linear_probe[:-1])
            else:
                assert False, task["labeler_type"]
        else:
            if task["labeler_type"] == "survival":
                replace(
                    params,
                    "EHRTransformer/~/SurvivalTask",
                    "code_weight_bias",
                    jnp.log2(jnp.array(batch_task["lambda"])),
                )

    else:
        rootLogger.error("Invalid task for postprocess?")
        exit()

    del batch_task

    non_fit_params = {}
    if args.start_from_checkpoint is not None:
        with open(os.path.join(args.start_from_checkpoint, "best"), "rb") as f:
            checkpointed_weights = pickle.load(f)

            if (
                task["type"] == "labeled_patients"
                and task["labeler_type"] == "survival"
                and ("EHRTransformer/~/SurvivalCLMBRTask/~/linear" in checkpointed_weights)
            ):
                magic_layer = checkpointed_weights["EHRTransformer/~/SurvivalCLMBRTask/~/linear"]
                checkpointed_weights["EHRTransformer/~/SurvivalTask/~/linear"] = magic_layer

            for p, v in list(params.items()):
                if p in checkpointed_weights:
                    value_to_replace = checkpointed_weights[p]
                    for name, value in v.items():
                        if name not in value_to_replace:
                            logging.error(
                                "Could not find value? %s %s",
                                p,
                                name,
                            )
                            exit()
                        if value_to_replace[name].shape != value.shape:
                            logging.error(
                                "Invalid shape %s %s %s %s",
                                p,
                                name,
                                value.shape,
                                value_to_replace[name].shape,
                            )
                            exit()

                    if args.freeze_weights:
                        non_fit_params[p] = checkpointed_weights[p]
                        del params[p]
                    else:
                        params[p] = checkpointed_weights[p]
                else:
                    logging.info("Have to train %s from scratch", p)
                    params[p] = params[p]

    original_non_fit_params = non_fit_params

    non_fit_params = femr.models.transformer.convert_params(non_fit_params, jnp.float16)
    non_fit_params = hk.data_structures.to_immutable_dict(non_fit_params)

    logging.info(
        "Done initing %s",
        str(jax.tree_map(lambda a: (a.shape, a.dtype), params)),
    )

    total_params = 0

    for a, a_w in params.items():
        for n, v in a_w.items():
            total = 1
            for i in v.shape:
                total *= i
            total_params += total

    logging.info("Total params %s", total_params)

    @functools.partial(
        jax.jit,
        static_argnums=(
            3,
            5,
        ),
    )
    def compute_loss(params, non_fit_params, rng, config, batch, requires_logits=False):
        total_params = params | hk.data_structures.to_mutable_dict(non_fit_params)
        loss, logits = model.apply(total_params, rng, config, batch, is_training=False)[:2]
        if requires_logits:
            return loss, logits
        else:
            return loss, None

    def compute_total_loss(split, params, non_fit_params, rng, config):
        if split == "dev" and args.dev_batches_path:
            split_to_eval = "train"
            loader_to_eval = dev_loader
        else:
            split_to_eval = split
            loader_to_eval = loader

        num_to_get = min(500, loader_to_eval.get_number_of_batches(split_to_eval))
        total_loss = 0
        total_indices = 0
        total_loss2 = 0
        total_indices2 = 0

        logits = []

        is_censor = []
        event_times = []
        labels = []

        for i in range(num_to_get):
            batch = loader_to_eval.get_batch(split_to_eval, i)
            if batch["num_indices"] == 0:
                print("Skipping ", i, " due to no indices")
                continue
            loss, logit = compute_loss(
                femr.models.transformer.convert_params(params, jnp.float16),
                non_fit_params,
                rng,
                config,
                batch,
                requires_logits=config["task"]["type"] == "labeled_patients",
            )
            total_loss += loss * batch["num_indices"]
            total_indices += batch["num_indices"]
            total_loss2 += loss
            total_indices2 += 1
            if config["task"]["type"] == "labeled_patients":
                if config["task"]["labeler_type"] == "survival":
                    logits.append(logit[: batch["num_indices"], :])
                    is_censor.append(batch["task"]["is_censor"][: batch["num_indices"]])
                    event_times.append(batch["task"]["event_times"][: batch["num_indices"]])
                else:
                    logits.append(logit[: batch["num_indices"]])
                    labels.append(batch["task"]["labels"][: batch["num_indices"]])

        loss = float(total_loss / total_indices)
        loss2 = float(total_loss2 / total_indices2)

        if config["task"]["type"] == "labeled_patients":
            if config["task"]["labeler_type"] == "survival":
                logits = jnp.concatenate(logits, axis=0)
                is_censor = jnp.concatenate(is_censor, axis=0)
                event_times = jnp.concatenate(event_times, axis=0)

                limit_time = jnp.quantile(event_times[~is_censor], 0.9)
                is_censor = is_censor.at[event_times > limit_time].set(True)
                event_times = event_times.at[event_times > limit_time].set(limit_time)

                c_statistic = femr.extension.metrics.compute_c_statistic(
                    event_times,
                    is_censor,
                    jnp.array(config["task"]["time_bins"]),
                    logits,
                )[0]
                other_statistics = {}
            elif config["task"]["labeler_type"] == "boolean":
                logits = jnp.concatenate(logits, axis=0)
                labels = jnp.concatenate(labels, axis=0)
                c_statistic = sklearn.metrics.roc_auc_score(labels, logits)
                other_statistics = {"aps": sklearn.metrics.average_precision_score(labels, logits)}
        else:
            c_statistic = -loss
            other_statistics = {}

        result = {"loss": loss, "loss2": loss2, "c_statistic": c_statistic, **other_statistics}

        return result

    @functools.partial(jax.value_and_grad)
    def loss_value_and_grad(params, non_fit_params, loss_scale, rng, config, batch):
        total_params = params | hk.data_structures.to_mutable_dict(non_fit_params)
        loss, _ = model.apply(total_params, rng, config, batch, is_training=True)[:2]

        assert loss.dtype == jnp.float32

        post_scale = loss_scale.scale(loss)
        return post_scale

    def apply_optimizer(params, grads, opt_state):
        updates, opt_state = opt.update(grads, opt_state, params)
        new_params = optax.apply_updates(params, updates)
        return new_params, opt_state

    @functools.partial(
        jax.jit,
        static_argnums=(5,),
        donate_argnums=(0, 2, 3, 4, 6),
    )
    def update(params, non_fit_params, loss_scale, rng, opt_state, config, batch):
        batch_loss, grads = loss_value_and_grad(
            femr.models.transformer.convert_params(params, jnp.float16),
            non_fit_params,
            loss_scale,
            rng,
            config,
            batch,
        )

        batch_loss = loss_scale.unscale(batch_loss.astype(jnp.float32))
        grads = loss_scale.unscale(femr.models.transformer.convert_params(grads, jnp.float32))

        grads_finite = jmp.all_finite(grads)

        loss_scale = loss_scale.adjust(grads_finite)

        opt_result = apply_optimizer(params, grads, opt_state)

        new_params, opt_state = jmp.select_tree(
            grads_finite,
            opt_result,
            (params, opt_state),
        )

        return new_params, opt_state, batch_loss, loss_scale

    def make_lr_schedule(warmup_percentage, total_steps):
        def lr_schedule(step):
            percent_complete = step / total_steps
            before_peak = jax.lax.convert_element_type((percent_complete <= warmup_percentage), jnp.float32)
            scale = (before_peak * (percent_complete / warmup_percentage) + (1 - before_peak)) * (1 - percent_complete)
            return scale

        return lr_schedule

    num_train_batches = loader.get_number_of_batches("train")

    total_steps = config["n_epochs"] * num_train_batches
    logging.info("total steps %s num train batches %s", total_steps, num_train_batches)

    def should_decay(module_name, name, value):
        return name not in ("b", "scale", "embeddings", "code_weight_bias")

    def mask_fn(params):
        return hk.data_structures.map(should_decay, params)

    logging.info("Applying decay mask %s", mask_fn(params))

    warmup = 1_000 / total_steps

    lr_schedule = make_lr_schedule(warmup_percentage=warmup, total_steps=total_steps)
    weight_decay = args.weight_decay
    logging.info("Using weight decay %s", weight_decay)
    opt = optax.chain(
        optax.clip_by_global_norm(config["max_grad_norm"]),
        optax.adamw(
            learning_rate=config["learning_rate"],
            weight_decay=config["weight_decay"],
            mask=mask_fn,
        ),
        optax.scale_by_schedule(lr_schedule),
    )
    opt_state = opt.init(params)

    loss_scale = jmp.DynamicLossScale(jnp.array(2**15, dtype=jnp.float32))

    best_loss = float("inf")

    logging.info("Starting loss scale %s", loss_scale)
    logging.info(
        "Starting train loss %s",
        compute_total_loss("train", params, non_fit_params, rng, config),
    )
    dev_loss = compute_total_loss("dev", params, non_fit_params, rng, config)
    logging.info("Starting dev loss %s", dev_loss)

    batches = femr.models.dataloader.Batches(
        data_path=args.data_path,
        batch_info_path=batch_info_path,
        seed=config["seed"] * 100,
        num_batch_threads=args.num_batch_threads,
        token_dropout=args.token_dropout,
        num_epochs=config["n_epochs"],
        num_batches=num_train_batches,
    )

    rng_sequence = hk.PRNGSequence(rng)

    per_limit = min(5000, num_train_batches)

    last_good = None

    while True:
        next_item = batches.get_next()
        if next_item is None:
            break

        batch, step = next_item

        if batch is None:
            continue

        if step % 100 == 0:
            logging.info(f"[Step {step}]")

        if (step % per_limit == 0 and step != 0) or step == 500 or step == 1500 or step == 2500:
            logging.info("Loss scale %s", loss_scale)
            logging.info(
                "Train loss %s",
                compute_total_loss("train", params, non_fit_params, rng, config),
            )
            dev_loss = compute_total_loss("dev", params, non_fit_params, rng, config)
            dev_loss_metric = -dev_loss["c_statistic"]
            logging.info("Dev loss %s", dev_loss)
            if dev_loss_metric != dev_loss_metric or (loss_scale.loss_scale == 1 and loss_scale.counter == 0):
                logging.info("Diverged, shutting down")
                break
            if dev_loss_metric < best_loss:
                last_good = step
                best_loss = dev_loss_metric
                test_loss = compute_total_loss("test", params, non_fit_params, rng, config)
                with open(os.path.join(args.directory, "best"), "wb") as out:
                    total_params = params | original_non_fit_params
                    pickle.dump(total_params, out)
                with open(os.path.join(args.directory, "best_opt_state"), "wb") as out:
                    pickle.dump(opt_state, out)
                with open(os.path.join(args.directory, "best_info"), "w") as out_t:
                    out_t.write(f"Step {step}, Loss {dev_loss}")
                with open(os.path.join(args.directory, "best_test_loss"), "w") as out_t:
                    json.dump(test_loss, out_t)
            elif args.early_stopping_window_steps is not None:
                if step - last_good > args.early_stopping_window_steps:
                    logging.info(
                        f"We haven't seen improvement in the dev loss in {args.early_stopping_window_steps}"
                        " steps, so apply early stopping"
                    )
                    break

            if args.max_iter is not None and step > args.max_iter:
                logging.info("Stopping due to max iter")
                break

            logging.info("Continuing to train ...")

        if args.freeze_weights and step == min(10000, num_train_batches * 15):
            logging.info("Swapping to full training")
            params = params | original_non_fit_params

            non_fit_params = hk.data_structures.to_immutable_dict({})
            original_non_fit_params = {}

            opt = optax.chain(
                optax.clip_by_global_norm(config["max_grad_norm"]),
                optax.adamw(
                    learning_rate=config["learning_rate"] / 20,
                    weight_decay=config["weight_decay"],
                    mask=mask_fn,
                ),
                optax.scale_by_schedule(lr_schedule),
            )
            opt_state = opt.init(params)

        # batch = jax.tree_map(lambda a: jax.device_put(a, device=jax.devices("gpu")[0]), batch)
        batch = jax.tree_map(lambda a: jax.device_put(a), batch)

        params, opt_state, batch_loss, loss_scale = update(
            params,
            non_fit_params,
            loss_scale,
            next(rng_sequence),
            opt_state,
            config,
            batch,
        )


def new_compute_representations() -> None:
    parser = argparse.ArgumentParser(prog="Compute representations")
    parser.add_argument("destination", type=str)
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--model_path", type=str, required=True)
    parser.add_argument("--prediction_times_path", type=str, required=False)
    parser.add_argument("--batch_size", type=int, default=(1 << 12), required=False)

    args = parser.parse_args()

    with open(os.path.join(args.model_path, "model", "config.msgpack"), "rb") as f:
        config = msgpack.load(f, use_list=False)

    random.seed(config["seed"])

    config = hk.data_structures.to_immutable_dict(config)

    with tempfile.TemporaryDirectory() as tmpdir:
        batches_path = os.path.join(tmpdir, "task_batches")

        command = f"clmbr_create_batches {tmpdir}/task_batches --data_path {args.data_path}"
        command += f" --task labeled_patients --labeled_patients_path {args.prediction_times_path} --val_start 70"
        command += f" --dictionary_path {args.model_path}/dictionary"
        if config["transformer"]["is_hierarchical"]:
            command += " --is_hierarchical"
        command += f" --transformer_vocab_size {config['transformer']['vocab_size']}"
        command += f" --batch_size {args.batch_size}"
        os.system(command)

        batch_info_path = os.path.join(batches_path, "batch_info.msgpack")

        database = femr.datasets.PatientDatabase(args.data_path)

        with open(os.path.join(args.model_path, "model", "best"), "rb") as f:
            params = pickle.load(f)

        params = femr.models.transformer.convert_params(params, dtype=jnp.float16)

        with open(batch_info_path, "rb") as f:
            batch_info = msgpack.load(f, use_list=False)

        with open(os.path.join(args.model_path, "model", "config.msgpack"), "rb") as f:
            config = msgpack.load(f, use_list=False)

        config = hk.data_structures.to_immutable_dict(config)

        random.seed(config["seed"])
        rng = jax.random.PRNGKey(42)

        assert batch_info["config"]["task"]["type"] == "labeled_patients"

        loader = femr.extension.dataloader.BatchLoader(args.data_path, batch_info_path)

        logging.info(
            "Loaded batches %s %s",
            loader.get_number_of_batches("train"),
            loader.get_number_of_batches("dev"),
        )

        def model_fn(config, batch):
            model = femr.models.transformer.EHRTransformer(config)(batch, no_task=True)
            return model

        dummy_batch = jax.tree_map(lambda a: jnp.array(a), loader.get_batch("train", 0))

        logging.info(
            "Got dummy batch %s",
            str(jax.tree_map(lambda a: (a.shape, a.dtype, a.device()), dummy_batch)),
        )

        model = hk.transform(model_fn)

        logging.info("Transformed the model function")

        logging.info(
            "Done initing %s",
            str(jax.tree_map(lambda a: (a.shape, a.dtype), params)),
        )

        print("Computing reps", datetime.datetime.now())

        @functools.partial(jax.jit, static_argnames=("config"))
        def compute_repr(params, rng, config, batch):
            repr, mask = model.apply(params, rng, config, batch)

            offsets = jnp.ones(
                (repr.shape[0], 1),
                dtype=repr.dtype,
            )
            return jnp.concatenate((repr, offsets), axis=-1)

        l_reprs = []
        l_repr_ages = []
        l_repr_pids = []
        l_repr_offsets = []

        for i, split in enumerate(("train", "dev", "test")):
            print("Starting batches", split, datetime.datetime.now())
            print("Starting to process", split, datetime.datetime.now())

            for j in range(loader.get_number_of_batches(split)):
                raw_batch = loader.get_batch(split, j)

                batch = jax.tree_map(lambda a: jax.device_put(a), raw_batch)

                repr = compute_repr(
                    params,
                    rng,
                    config,
                    batch,
                )

                def slice(val):
                    if len(val.shape) == 3:
                        return val[: batch["num_indices"], :, :]
                    if len(val.shape) == 2:
                        return val[: batch["num_indices"], :]
                    elif len(val.shape) == 1:
                        return val[: batch["num_indices"]]

                p_index = batch["transformer"]["label_indices"] // batch["transformer"]["length"]
                p_index = slice(p_index)

                l_reprs.append(slice(repr))
                assert repr.dtype == jnp.float16
                l_repr_ages.append(
                    raw_batch["transformer"]["integer_ages"][slice(batch["transformer"]["label_indices"])]
                )
                l_repr_pids.append(raw_batch["patient_ids"][p_index])
                l_repr_offsets.append(raw_batch["offsets"][p_index])

        print("About to concat 1", datetime.datetime.now())
        reprs = jnp.concatenate(l_reprs, axis=0)
        print("About to concat 2", datetime.datetime.now())
        repr_ages = np.concatenate(l_repr_ages, axis=0)
        print("About to concat 3", datetime.datetime.now())

        assert repr_ages.dtype == np.uint32
        repr_pids = np.concatenate(l_repr_pids, axis=0)
        assert repr_pids.dtype == np.int64
        repr_offsets = np.concatenate(l_repr_offsets, axis=0)

        label_pids = np.array([val[0] for val in batch_info["config"]["task"]["labels"]], dtype=np.int64)
        label_ages = np.array([val[1] for val in batch_info["config"]["task"]["labels"]], dtype=np.uint32)
        label_values = np.array([val[2] for val in batch_info["config"]["task"]["labels"]])

        print("About to sort labels", datetime.datetime.now(), len(label_pids))

        sort_indices = np.lexsort((label_ages, label_pids))

        print("Done sorting labels", datetime.datetime.now())

        label_pids = label_pids[sort_indices]
        label_ages = label_ages[sort_indices]
        label_values = label_values[sort_indices]

        repr_offsets = repr_offsets.astype(np.int32)

        print("About to sort representations", datetime.datetime.now(), len(repr_offsets))
        sort_indices = np.lexsort((-repr_offsets, repr_ages, repr_pids))
        print("Done sorting representations", datetime.datetime.now())

        repr_offsets = repr_offsets[sort_indices].astype(np.uint32)
        repr_ages = repr_ages[sort_indices]
        repr_pids = repr_pids[sort_indices]

        matching_indices = femr.extension.dataloader.compute_repr_label_alignment(
            label_pids, label_ages, repr_pids, repr_ages, repr_offsets
        )

        print("Done sorting labels and representations", datetime.datetime.now())

        assert np.all(repr_ages[matching_indices] <= label_ages)
        assert np.all(repr_pids[matching_indices] == label_pids)

        print("Creating representations")

        reprs = np.array(reprs[sort_indices[matching_indices], :])

        print("Computing dates")

        prediction_dates = []
        for pid, age in zip(label_pids, label_ages):
            birth_date = datetime.datetime.combine(database.get_patient_birth_date(pid), datetime.time.min)
            prediction_dates.append(birth_date + datetime.timedelta(minutes=int(age)))

        print("About to save", datetime.datetime.now())

        result = {
            "representations": np.stack(reprs),
            "patient_ids": np.array(label_pids),
            "prediction_times": np.array(np.array(prediction_dates)),
        }

        with open(args.destination, "wb") as wf:
            pickle.dump(result, wf)


def compute_representations() -> None:
    parser = argparse.ArgumentParser(prog="Compute representations")
    parser.add_argument("destination", type=str)
    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--batches_path", type=str, required=True)
    parser.add_argument("--model_dir", type=str, required=True)

    args = parser.parse_args()

    with open(os.path.join(args.model_dir, "config.msgpack"), "rb") as f:
        config = msgpack.load(f, use_list=False)

    random.seed(config["seed"])

    config = hk.data_structures.to_immutable_dict(config)
    batch_info_path = os.path.join(args.batches_path, "batch_info.msgpack")

    loader = femr.extension.dataloader.BatchLoader(args.data_path, batch_info_path)

    def model_fn(config, batch):
        model = femr.models.transformer.EHRTransformer(config)(batch, no_task=True)
        return model

    dummy_batch = loader.get_batch("train", 0)
    dummy_batch = jax.tree_map(lambda a: jnp.array(a), dummy_batch)

    rng = jax.random.PRNGKey(42)
    model = hk.transform(model_fn)

    with open(os.path.join(args.model_dir, "best"), "rb") as f:
        params = pickle.load(f)

    @functools.partial(jax.jit, static_argnames="config")
    def compute_repr(params, rng, config, batch):
        return model.apply(params, rng, config, batch)

    database = femr.datasets.PatientDatabase(args.data_path)

    results = []

    for split in ("train", "dev", "test"):
        for dev_index in range(loader.get_number_of_batches(split)):
            raw_batch = loader.get_batch(split, dev_index)
            batch = jax.tree_map(lambda a: jnp.array(a), raw_batch)

            repr, mask = compute_repr(
                femr.models.transformer.convert_params(params, dtype=jnp.float16),
                rng,
                config,
                batch,
            )

            repr = np.array(repr)

            p_index = batch["transformer"]["label_indices"] // batch["transformer"]["length"]

            for i in range(batch["num_indices"]):
                r = repr[i, :]

                label_pid = raw_batch["patient_ids"][p_index[i]]
                label_age = raw_batch["task"]["label_ages"][i]
                label_value = raw_batch["task"]["labels"][i]

                offset = raw_batch["offsets"][p_index[i]]
                results.append((label_pid, label_age, offset, r, label_value))

    results.sort(key=lambda a: a[:3])

    label_times = []

    data_matrix = []
    label_pids = []
    label_ages = []
    label_values = []

    last_label_idx = None

    for pid, age, offset, r, label_value in results:
        # Ignore duplicate
        if (pid, age) == last_label_idx:
            continue
        last_label_idx = (pid, age)

        birth_date = datetime.datetime.combine(database.get_patient_birth_date(pid), datetime.time.min)
        label_time = birth_date + datetime.timedelta(minutes=int(age))
        label_times.append(label_time)
        data_matrix.append(r)
        label_pids.append(pid)
        label_ages.append(age)
        label_values.append(label_value)

    result = {
        "data_path": args.data_path,
        "model": args.model_dir,
        "data_matrix": np.stack(data_matrix),
        "patient_ids": np.array(label_pids),
        "labeling_time": np.array(label_times),
        "label_values": np.array(label_values),
    }

    with open(args.destination, "wb") as wf:
        pickle.dump(result, wf)
