"""
纯逻辑单元测试：时间/速度解析、容器兼容检查、编码器参数生成、进度换算。

这些测试不依赖 FFmpeg 二进制，可在任意机器上运行。
"""

import pytest

import ffmpeg_utils as fu
from config import IS_WINDOWS


class TestParseTime:
    def test_hms(self):
        assert fu.parse_time("01:02:03.5") == pytest.approx(3723.5)

    def test_ms(self):
        assert fu.parse_time("02:30") == 150.0

    def test_seconds(self):
        assert fu.parse_time("12.5") == 12.5

    def test_empty_and_invalid(self):
        assert fu.parse_time("") == 0.0
        assert fu.parse_time(None) == 0.0
        assert fu.parse_time("garbage") == 0.0


class TestOutputExt:
    def test_common(self):
        assert fu.output_ext(r"C:\videos\a.MP4") == "mp4"
        assert fu.output_ext("/v/a.mkv") == "mkv"

    def test_no_ext(self):
        assert fu.output_ext("/v/a") == ""
        assert fu.output_ext("") == ""


class TestSameFile:
    def test_same_path(self, tmp_path):
        f = tmp_path / "a.mp4"
        f.write_bytes(b"x")
        assert fu.same_file(str(f), str(f))
        assert not fu.same_file(str(f), str(tmp_path / "b.mp4"))

    @pytest.mark.skipif(not IS_WINDOWS, reason="大小写不敏感仅在 Windows 上验证")
    def test_case_insensitive_on_windows(self, tmp_path):
        f = tmp_path / "Clip.mp4"
        f.write_bytes(b"x")
        assert fu.same_file(str(f), str(f).upper())


def _audio_src(codec):
    return {"streams": [{"codec_type": "audio", "codec_name": codec}]}


def _video_src(codec):
    return {"streams": [{"codec_type": "video", "codec_name": codec}]}


class TestEnsureAudioCompatible:
    def test_non_copy_untouched(self):
        assert fu.ensure_audio_compatible("mp4", "aac", "192k", _audio_src("flac")) == \
            ("aac", "192k", False, "")

    def test_no_audio_stream(self):
        acodec, br, switched, note = fu.ensure_audio_compatible("mp4", "copy", "", {"streams": []})
        assert acodec == "copy" and switched is False

    def test_compatible_copy_kept(self):
        assert fu.ensure_audio_compatible("mp4", "copy", "", _audio_src("aac")) == \
            ("copy", "", False, "")

    def test_incompatible_switched_to_aac(self):
        acodec, br, switched, note = fu.ensure_audio_compatible(
            "mp4", "copy", "", _audio_src("vorbis"))
        assert acodec == "aac" and br == "192k" and switched
        assert "vorbis" in note

    def test_incompatible_switched_to_opus_webm(self):
        acodec, br, switched, note = fu.ensure_audio_compatible(
            "webm", "copy", "", _audio_src("aac"))
        assert acodec == "libopus" and br == "128k" and switched

    def test_unlisted_container_kept(self):
        assert fu.ensure_audio_compatible("mkv", "copy", "", _audio_src("vorbis"))[0] == "copy"


class TestCheckCopyVideoCompat:
    def test_same_container_ok(self):
        assert fu.check_copy_video_compat("mp4", _video_src("h264")) is None

    def test_reject_webm_with_h264(self):
        err = fu.check_copy_video_compat("webm", _video_src("h264"))
        assert err and "H264" in err

    def test_unlisted_container_ok(self):
        assert fu.check_copy_video_compat("mkv", _video_src("anything")) is None


class TestBuildEncoder:
    def test_software_crf_and_preset(self):
        enc, pre, vf, args = fu.build_encoder("software", "h264", 20, "", "slow", "")
        assert enc == "libx264" and pre == [] and vf == []
        assert args == ["-crf", "20", "-preset", "slow"]

    def test_software_svtav1_no_preset(self):
        enc, pre, vf, args = fu.build_encoder("software", "av1", 23, "5M", "medium", "")
        assert enc == "libsvtav1" and args == ["-b:v", "5M"]

    def test_software_scale(self):
        enc, pre, vf, args = fu.build_encoder("software", "h265", 23, "", "medium", "-2:720")
        assert vf == ["scale=-2:720"]

    def test_nvenc_cq_and_preset(self):
        enc, pre, vf, args = fu.build_encoder("nvenc", "h265", 24, "", "p5", "1280:720")
        assert enc == "hevc_nvenc"
        assert args == ["-cq", "24", "-preset", "p5"]
        assert vf == ["scale_cuda=1280:720"]

    def test_nvenc_bitrate(self):
        enc, pre, vf, args = fu.build_encoder("nvenc", "h264", 23, "8M", "p4", "")
        assert args == ["-b:v", "8M", "-maxrate", "8M", "-bufsize", "8M", "-preset", "p4"]

    def test_qsv(self):
        enc, pre, vf, args = fu.build_encoder("qsv", "h264", 25, "", "fast", "1920:1080")
        assert enc == "h264_qsv"
        assert "-global_quality" in args and "-preset" in args
        assert vf == ["scale_qsv=1920:1080"]

    def test_amf(self):
        enc, pre, vf, args = fu.build_encoder("amf", "h264", 22, "", "quality", "")
        assert enc == "h264_amf"
        assert args == ["-rc", "cqp", "-qp_i", "22", "-qp_p", "22", "-quality", "quality"]

    def test_unsupported_combos(self):
        assert fu.build_encoder("amf", "av1", 23, "", "balanced", "")[0] is None
        assert fu.build_encoder("nope", "h264", 23, "", "medium", "")[0] is None

    def test_vaapi_requires_linux(self, monkeypatch):
        monkeypatch.setattr(fu, "IS_LINUX", False)
        assert fu.build_encoder("vaapi", "h264", 23, "", "", "")[0] is None

    def test_vaapi_filter_order(self, monkeypatch):
        monkeypatch.setattr(fu, "IS_LINUX", True)
        enc, pre, vf, args = fu.build_encoder("vaapi", "h265", 25, "", "", "-2:720")
        assert enc == "hevc_vaapi"
        assert pre == ["-vaapi_device", fu.VAAPI_DEVICE]
        assert vf == ["format=nv12", "hwupload", "scale_vaapi=-2:720"]


class TestProgressMath:
    def test_progress(self):
        assert fu.compute_progress(5, 10) == pytest.approx(50.0)
        assert fu.compute_progress(30, 10) == 99.9
        assert fu.compute_progress(5, 0) is None
        assert fu.compute_progress(None, 10) is None

    def test_speed_parse(self):
        assert fu.parse_speed("2.5x") == pytest.approx(2.5)
        assert fu.parse_speed("N/A") == 0.0
        assert fu.parse_speed("") == 0.0

    def test_eta(self):
        assert fu.compute_eta(2, 10, 2.0) == pytest.approx(4.0)
        assert fu.compute_eta(11, 10, 2.0) == 0.0
        assert fu.compute_eta(2, 10, 0) is None
