# rpmdeplint

Rpmdeplint is a tool to find errors in RPM packages in the context of their
dependency graph.

## Requirements

- Python 3.8

## External Dependencies

In order to run the tool, the following pre-requisites need to be installed:

- [rpm](http://rpm.org)
- [librepo](https://github.com/rpm-software-management/librepo)
- [hawkey](https://github.com/rpm-software-management/hawkey)
- [libsolv](https://github.com/openSUSE/libsolv)

For development and tests:

- [sphinx](https://www.sphinx-doc.org)
- [rpmfluff](https://pagure.io/rpmfluff)

## Documentation

- https://rpmdeplint.readthedocs.io

## Using

A user guide is provided by the man(1) page shipped with this tool:

    man rpmdeplint
