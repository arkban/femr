## These commands worked for me to build the project:

- `brew install bazellisk`

- `python3.10 -m venv <path to venv>`

- Change `bazel` to `bazelisk` in setup.py

- Change the rules for boost in the WORKSPACE file
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

- `USE_BAZEL_VERSION=6.0.0 MACOSX_DEPLOYMENT_TARGET=14.2 pip install -e .`

## To get the tests to run in intellij
- `pip3 install --upgrade setuptools`

- `USE_BAZEL_VERSION=6.0.0 MACOSX_DEPLOYMENT_TARGET=14.2 pip install -e '.[build]'

- `USE_BAZEL_VERSION=6.0.0 MACOSX_DEPLOYMENT_TARGET=14.2 pip install -e '.[model]'`

- `pip uninstall jaxlib`

- `pip install jaxlib==0.4.7`

- `pytest tests -V` 