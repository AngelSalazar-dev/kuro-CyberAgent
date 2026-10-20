# run.ps1 - run Kuro straight from the checkout on Windows.
#
# The real launcher lives in packaging/; it needs KURO_ROOT to point at the
# checkout (its own directory is packaging/). This shim does exactly that, so
# `.\run.ps1` starts the proxy and the harness for you. The launcher then uses
# the checkout's own venv (.\.venv) when it exists, so there is nothing to
# activate:
#
#   python -m venv .venv && .\.venv\Scripts\Activate.ps1
#   pip install -r requirements.txt
#   .\setup.ps1                          # verify the route end-to-end
#   .\run.ps1                            # launch
#
# If you installed with install.ps1, a `kuro` command is already on your PATH
# and this shim is not needed.
#
# (The repo root cannot be named `kuro` - that name belongs to the Python
# package directory - hence run.ps1.)
#
# See packaging\kuro.ps1 for the configuration knobs.

$ErrorActionPreference = 'Stop'

$self_dir = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$env:KURO_ROOT = $self_dir
& "$self_dir\packaging\kuro.ps1" $args
exit $LASTEXITCODE

