# Compatibility Changes

- Changed to use Python 3.10 in [`pyproject.toml`](./pyproject.toml)
- Changed the rules for boost in [`WORKSPACE`](native/WORKSPACE) according to [rules-boost](https://github.com/nelhage/rules_boost).
  ```
  http_archive(
      name = "com_github_nelhage_rules_boost",

      # Replace the commit hash in both places (below) with the latest, rather than using the stale one here.
      # Even better, set up Renovate and let it do the work for you (see "Suggestion: Updates" in the README).
      url = "https://github.com/nelhage/rules_boost/archive/96e9b631f104b43a53c21c87b01ac538ad6f3b48.tar.gz",
      strip_prefix = "rules_boost-96e9b631f104b43a53c21c87b01ac538ad6f3b48",
      # When you first run this tool, it'll recommend a sha256 hash to put here with a message like: "DEBUG: Rule 'com_github_nelhage_rules_boost' indicated that a canonical reproducible form can be obtained by modifying arguments sha256 = ..."
  )
  ```
- Added explicit dependency on `jaxlib` version to `0.4.7` in in [`pyproject.toml`](./pyproject.toml) to avoid runtime error:
    ```plain
    RuntimeError: jaxlib version 0.4.23 is newer than and incompatible with jax version 0.4.8. Please update your jax and/or jaxlib packages.
    ```

# Python Code

Python code lives in these directories:

- [`build`](./build)
- [`src`](./src)
- [`tests`](./tests)

## Install (for) Python code

> [!NOTE]
> These need to be done only once, and must be done inside the local git clone!

- Setup `direnv`:
    - Run `brew install direnv`
    - Run `direnv allow .`
- Setup `bazelisk`:
    - Run `brew install bazelisk`
    - Run `bazelisk`
        - (This may take a while)
- Setup `pre-commit`:
    - Run `brew install pre-commit`
    - Run `pre-commit install`
- Setup Python
    - Run `brew install pyenv` to install `pyenv`
    - [Set up your shell environment for Pyenv](https://github.com/pyenv/pyenv?tab=readme-ov-file#set-up-your-shell-environment-for-pyenv)
    - Run `pyenv install` to install the right version of Python
        - (This will source the version from [`.python-version`](./.python-version))
- Configure virtual environment
    - Run `python -m venv $VENV_DIR` to create virtual environment
        - (See [Create a new virtual environment](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/#create-a-new-virtual-environment))
    - Run `source $VENV_DIR/bin/activate`
        - (See [Activate a virtual environment](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/#activate-a-virtual-environment))
- Downloads dependencies
    - Run `pip install --upgrade pip setuptools`
    - Run
        ```shell
        pip install --editable '.' && \
          pip install --editable '.[build]' && \
          pip install --editable '.[models]'
        ```
        - Documentation on [`--editable` mode](https://setuptools.pypa.io/en/latest/userguide/development_mode.html)
        - Note you CANNOT install these packages in one command, `pip` gets confused and thinks you are trying to install `femr`

## Building Python Code

> [!NOTE]
> If you have `direnv` setup correctly,
> whenever you enter this directory it will automatically `source $VENV_DIR/bin/activate` for you!

- Run `python setup.py build`

## Testing Python code

- Run `pytest tests`
