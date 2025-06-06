import base64
import os
from argparse import Namespace

import pytest

from pdm._types import RepositoryConfig
from pdm.cli.commands.publish import Command as PublishCommand
from pdm.cli.commands.publish.package import PackageFile
from pdm.cli.commands.publish.repository import Repository
from pdm.exceptions import PdmUsageError
from tests import FIXTURES

pytestmark = pytest.mark.usefixtures("mock_run_gpg")


@pytest.mark.parametrize(
    "filename",
    ["demo-0.0.1-py2.py3-none-any.whl", "demo-0.0.1.tar.gz", "demo-0.0.1.zip"],
)
def test_package_parse_metadata(filename):
    fullpath = FIXTURES / "artifacts" / filename
    package = PackageFile.from_filename(str(fullpath), None)
    assert package.base_filename == filename
    meta = package.metadata_dict
    assert meta["name"] == "demo"
    assert meta["version"] == "0.0.1"
    assert all(f"{hash_name}_digest" in meta for hash_name in ["md5", "sha256", "blake2_256"])

    if filename.endswith(".whl"):
        assert meta["pyversion"] == "py2.py3"
        assert meta["filetype"] == "bdist_wheel"
    else:
        assert meta["pyversion"] == "source"
        assert meta["filetype"] == "sdist"


def test_parse_metadata_with_non_ascii_chars():
    fullpath = FIXTURES / "artifacts" / "caj2pdf-restructured-0.1.0a6.tar.gz"
    package = PackageFile.from_filename(str(fullpath), None)
    meta = package.metadata_dict
    assert meta["summary"] == "caj2pdf 重新组织，方便打包与安装"  # noqa: RUF001
    assert meta["author_email"] == "张三 <san@zhang.me>"
    assert meta["description"].strip() == "# caj2pdf\n\n测试中文项目"


def test_package_add_signature(tmp_path):
    package = PackageFile.from_filename(str(FIXTURES / "artifacts/demo-0.0.1-py2.py3-none-any.whl"), None)
    tmp_path.joinpath("signature.asc").write_bytes(b"test gpg signature")
    package.add_gpg_signature(str(tmp_path / "signature.asc"), "signature.asc")
    assert package.gpg_signature == ("signature.asc", b"test gpg signature")


def test_package_call_gpg_sign():
    package = PackageFile.from_filename(str(FIXTURES / "artifacts/demo-0.0.1-py2.py3-none-any.whl"), None)
    try:
        package.sign(None)
    finally:
        try:
            os.unlink(package.filename + ".asc")
        except OSError:
            pass
    assert package.gpg_signature == (package.base_filename + ".asc", b"fake signature")


def test_repository_get_release_urls(project):
    package_files = [
        PackageFile.from_filename(str(FIXTURES / "artifacts" / fn), None)
        for fn in [
            "demo-0.0.1-py2.py3-none-any.whl",
            "demo-0.0.1.tar.gz",
            "demo-0.0.1.zip",
        ]
    ]
    config = RepositoryConfig(
        config_prefix="repository",
        name="test",
        url="https://upload.pypi.org/legacy/",
        username="abc",
        password="123",
    )
    repository = Repository(project, config)
    assert repository.get_release_urls(package_files) == {"https://pypi.org/project/demo/0.0.1/"}

    repository.url = "https://example.pypi.org/legacy/"
    assert not repository.get_release_urls(package_files)


@pytest.mark.usefixtures("prepare_packages")
def test_publish_pick_up_asc_files(project, uploaded, pdm):
    for p in list(project.root.joinpath("dist").iterdir()):
        with open(str(p) + ".asc", "w") as f:
            f.write("fake signature")

    pdm(
        ["publish", "--no-build", "--username=abc", "--password=123"],
        obj=project,
        strict=True,
    )
    # Test wheels are uploaded first
    assert uploaded[0].base_filename.endswith(".whl")
    for package in uploaded:
        assert package.gpg_signature == (
            package.base_filename + ".asc",
            b"fake signature",
        )


@pytest.mark.usefixtures("prepare_packages")
def test_publish_package_with_signature(project, uploaded, pdm):
    pdm(
        ["publish", "--no-build", "-S", "--username=abc", "--password=123"],
        obj=project,
        strict=True,
    )
    for package in uploaded:
        assert package.gpg_signature == (
            package.base_filename + ".asc",
            b"fake signature",
        )


@pytest.mark.usefixtures("local_finder")
def test_publish_and_build_in_one_run(fixture_project, pdm, mock_pypi):
    project = fixture_project("demo-module")
    result = pdm(["publish", "--username=abc", "--password=123"], obj=project, strict=True).output

    mock_pypi.assert_called()
    assert "Uploading demo_module-0.1.0-py3-none-any.whl" in result
    assert "Uploading demo_module-0.1.0.tar.gz" in result, result
    assert "https://pypi.org/project/demo-module/0.1.0/" in result


def test_publish_cli_args_and_env_var_precedence(project, monkeypatch, mocker):
    repository = mocker.patch.object(Repository, "__init__", return_value=None)
    PublishCommand.get_repository(
        project,
        Namespace(
            repository=None,
            username="foo",
            password="bar",
            ca_certs="custom.pem",
            verify_ssl=True,
        ),
    )
    repository.assert_called_with(
        project,
        RepositoryConfig(
            config_prefix="repository",
            name="pypi",
            url="https://upload.pypi.org/legacy/",
            username="foo",
            password="bar",
            ca_certs="custom.pem",
            verify_ssl=None,
        ),
    )

    with monkeypatch.context() as m:
        m.setenv("PDM_PUBLISH_USERNAME", "bar")
        m.setenv("PDM_PUBLISH_PASSWORD", "secret")
        m.setenv("PDM_PUBLISH_REPO", "testpypi")
        m.setenv("PDM_PUBLISH_CA_CERTS", "override.pem")

        PublishCommand.get_repository(
            project,
            Namespace(
                repository=None,
                username=None,
                password=None,
                ca_certs=None,
                verify_ssl=True,
            ),
        )
        repository.assert_called_with(
            project,
            RepositoryConfig(
                config_prefix="repository",
                name="testpypi",
                url="https://test.pypi.org/legacy/",
                username="bar",
                password="secret",
                ca_certs="override.pem",
                verify_ssl=None,
            ),
        )

        PublishCommand.get_repository(
            project,
            Namespace(
                repository="pypi",
                username="foo",
                password=None,
                ca_certs="custom.pem",
                verify_ssl=True,
            ),
        )
        repository.assert_called_with(
            project,
            RepositoryConfig(
                config_prefix="repository",
                name="pypi",
                url="https://upload.pypi.org/legacy/",
                username="foo",
                password="secret",
                ca_certs="custom.pem",
                verify_ssl=None,
            ),
        )


def test_repository_get_credentials_from_keyring(project, keyring, mocker):
    keyring.save_auth_info("https://test.org/upload", "foo", "barbaz")
    config = RepositoryConfig(config_prefix="repository", name="test", url="https://test.org/upload")
    basic_auth = mocker.patch("httpx.BasicAuth.__init__", return_value=None)
    Repository(project, config)
    basic_auth.assert_called_with(username="foo", password="barbaz")


def test_repository_get_token_from_oidc(project, mocker, httpx_mock):
    minted_token = "minted_oidc_token"
    test_pypi_url = "https://test.org/upload"
    httpx_mock.add_response(
        url="https://test.org/_/oidc/audience",
        method="GET",
        json={"audience": "testpypi"},
    )
    httpx_mock.add_response(
        url="https://test.org/_/oidc/mint-token",
        method="POST",
        json={"token": minted_token},
    )
    config = RepositoryConfig(config_prefix="repository", name="test", url=test_pypi_url)
    detect_credential_mock = mocker.patch(
        "pdm.cli.commands.publish.repository.detect_credential",
        return_value="A_OIDC_TOKEN",
    )
    repository = Repository(project, config=config)
    detect_credential_mock.assert_called_once()
    assert base64.b64decode(repository.session.auth._auth_header[6:]).decode("utf-8") == f"__token__:{minted_token}"


def test_repository_get_token_from_oidc_request_error(project, mocker, httpx_mock, capsys):
    test_pypi_url = "https://test.org/upload"
    httpx_mock.add_response(
        url="https://test.org/_/oidc/audience",
        method="GET",
        status_code=502,
    )

    config = RepositoryConfig(config_prefix="repository", name="test", url=test_pypi_url)
    with pytest.raises(PdmUsageError):
        Repository(project, config=config)
    captured = capsys.readouterr()
    assert "Failed to get PyPI token via OIDC" in captured.err


def test_repository_get_token_from_oidc_unsupported_platform(project, mocker, httpx_mock, capsys):
    test_pypi_url = "https://test.org/upload"
    httpx_mock.add_response(
        url="https://test.org/_/oidc/audience",
        method="GET",
        json={"audience": "testpypi"},
    )

    config = RepositoryConfig(config_prefix="repository", name="test", url=test_pypi_url)
    detect_credential_mock = mocker.patch(
        "pdm.cli.commands.publish.repository.detect_credential",
        return_value=None,
    )
    with pytest.raises(PdmUsageError):
        Repository(project, config=config)
    captured = capsys.readouterr()
    assert "This platform is not supported for trusted publishing via OIDC" in captured.err
    detect_credential_mock.assert_called_once()


def test_repository_get_token_misconfigured_github(project, monkeypatch, capsys, httpx_mock):
    test_pypi_url = "https://test.org/upload"
    httpx_mock.add_response(
        url="https://test.org/_/oidc/audience",
        method="GET",
        json={"audience": "testpypi"},
    )

    config = RepositoryConfig(config_prefix="repository", name="test", url=test_pypi_url)
    # set env variable to get `id`` into detect_github function
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    with pytest.raises(PdmUsageError):
        Repository(project, config=config)
    captured = capsys.readouterr()
    assert "Unable to detect OIDC token for CI platform:" in captured.err
