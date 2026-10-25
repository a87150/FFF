"""合并接口参数校验（纯校验逻辑，不依赖真实 ffmpeg / ffprobe）"""

import pytest

from app import create_app


@pytest.fixture()
def client():
    return create_app().test_client()


@pytest.fixture()
def files(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    return a, b


def post(client, payload):
    return client.post("/api/merge", json=payload)


class TestMergeValidation:
    def test_inputs_must_be_list(self, client):
        r = post(client, {"inputs": "C:/v/a.mp4", "output": "C:/v/o.mp4"})
        assert r.status_code == 400
        assert "数组" in r.get_json()["error"]

    def test_needs_two_files(self, client, files):
        a, _ = files
        r = post(client, {"inputs": [str(a)], "output": "C:/v/o.mp4"})
        assert r.status_code == 400
        assert "2 个文件" in r.get_json()["error"]

    def test_missing_input_rejected(self, client, files, tmp_path):
        a, _ = files
        r = post(client, {"inputs": [str(a), str(tmp_path / "nope.mp4")],
                          "output": str(tmp_path / "o.mp4")})
        assert r.status_code == 400
        assert "不存在" in r.get_json()["error"]

    def test_missing_output(self, client, files):
        a, b = files
        r = post(client, {"inputs": [str(a), str(b)]})
        assert r.status_code == 400
        assert "输出路径" in r.get_json()["error"]

    def test_output_without_extension(self, client, files, tmp_path):
        a, b = files
        r = post(client, {"inputs": [str(a), str(b)], "output": str(tmp_path / "out")})
        assert r.status_code == 400
        assert "扩展名" in r.get_json()["error"]

    def test_output_cannot_overwrite_input(self, client, files):
        a, b = files
        r = post(client, {"inputs": [str(a), str(b)], "output": str(a)})
        assert r.status_code == 400
        assert "不能和输入相同" in r.get_json()["error"]

    def test_existing_output_needs_overwrite(self, client, files, tmp_path):
        a, b = files
        out = tmp_path / "out.mp4"
        out.write_bytes(b"x")
        r = post(client, {"inputs": [str(a), str(b)], "output": str(out)})
        assert r.status_code == 409
        assert r.get_json()["exists"] is True

    def test_smart_platform_rejected(self, client, files, tmp_path):
        a, b = files
        r = post(client, {"inputs": [str(a), str(b)],
                          "output": str(tmp_path / "out.mp4"), "platform": "smart"})
        assert r.status_code == 400
        assert "智能模式" in r.get_json()["error"]

    def test_webm_requires_vp9_or_av1(self, client, files, tmp_path):
        a, b = files
        r = post(client, {"inputs": [str(a), str(b)],
                          "output": str(tmp_path / "out.webm"),
                          "platform": "software", "format": "h264"})
        assert r.status_code == 400
        assert "WebM" in r.get_json()["error"]

    def test_invalid_scale_rejected(self, client, files, tmp_path):
        a, b = files
        r = post(client, {"inputs": [str(a), str(b)],
                          "output": str(tmp_path / "out.mp4"), "scale": "abc"})
        assert r.status_code == 400
        assert "缩放" in r.get_json()["error"]

    def test_invalid_bitrate_rejected(self, client, files, tmp_path):
        a, b = files
        r = post(client, {"inputs": [str(a), str(b)],
                          "output": str(tmp_path / "out.mp4"), "bitrate": "abc"})
        assert r.status_code == 400
        assert "码率" in r.get_json()["error"]
