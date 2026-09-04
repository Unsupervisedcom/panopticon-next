"""The release workflow publishes one version-aligned Python and container release."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text()


def test_published_release_builds_image_from_the_release_artifact() -> None:
    assert "release:\n    types:\n      - published" in RELEASE_WORKFLOW
    image_job = RELEASE_WORKFLOW.split("  publish-image:\n", 1)[1]
    assert "needs: build" in image_job
    assert "packages: write" in image_job
    assert "actions/download-artifact@v4" in image_job


def test_image_uses_and_verifies_the_new_release_version() -> None:
    image_job = RELEASE_WORKFLOW.split("  publish-image:\n", 1)[1]
    assert 'version="${GITHUB_REF_NAME#v}"' in image_job
    assert 'version("panopticon-next")' in image_job
    assert "from panopticon.sessionservice.images import _base_fingerprint" in image_job
    assert "PANOPTICON_VERSION=$VERSION" in image_job
    assert "PANOPTICON_WHEEL=$WHEEL" in image_job
    assert '--entrypoint panopticon "$IMAGE:$VERSION-smoke" --version' in image_job
    assert '"panopticon $VERSION"' in image_job


def test_image_is_published_to_ghcr_for_both_supported_architectures() -> None:
    image_job = RELEASE_WORKFLOW.split("  publish-image:\n", 1)[1]
    assert "IMAGE=ghcr.io/${GITHUB_REPOSITORY,,}" in image_job
    assert "docker/login-action@v3" in image_job
    assert "docker/build-push-action@v6" in image_job
    assert "platforms: linux/amd64,linux/arm64" in image_job
    assert "push: true" in image_job
    assert "type=raw,value=${{ env.VERSION }}" in image_job
    assert "type=raw,value=latest,enable=${{ !github.event.release.prerelease }}" in image_job
