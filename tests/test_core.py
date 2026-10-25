"""
纯逻辑单元测试：时间/速度解析、容器兼容检查、编码器参数生成、进度换算、目录探查。

这些测试不依赖 FFmpeg 二进制，可在任意机器上运行。
"""

import os

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


class TestScanVideos:
    def _make_tree(self, tmp_path):
        (tmp_path / "sub" / "deep").mkdir(parents=True)
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "a.mp4").write_bytes(b"x")
        (tmp_path / "b.txt").write_bytes(b"x")
        (tmp_path / "sub" / "c.mkv").write_bytes(b"x")
        (tmp_path / "sub" / "deep" / "d.mov").write_bytes(b"x")
        (tmp_path / ".hidden" / "e.mp4").write_bytes(b"x")
        (tmp_path / ".dotfile.mp4").write_bytes(b"x")

    def _rels(self, items):
        return [it["rel"].replace("\\", "/") for it in items]

    def test_recursive_and_sorted(self, tmp_path):
        self._make_tree(tmp_path)
        items, truncated = fu.scan_videos(str(tmp_path))
        assert self._rels(items) == ["a.mp4", "sub/c.mkv", "sub/deep/d.mov"]
        assert truncated is False
        assert all(os.path.isabs(it["path"]) for it in items)
        assert all(it["name"] in it["rel"] for it in items)
        assert items[0]["size"] == 1 and items[0]["size_human"]

    def test_max_files_truncates(self, tmp_path):
        for i in range(5):
            (tmp_path / f"v{i}.mp4").write_bytes(b"x")
        items, truncated = fu.scan_videos(str(tmp_path), max_files=3)
        assert len(items) == 3
        assert truncated is True

    def test_empty_dir(self, tmp_path):
        items, truncated = fu.scan_videos(str(tmp_path))
        assert items == [] and truncated is False


# ==================== 合并 ====================

def _merge_info(duration=10.0, vcodec="h264", width=1920, height=1080,
                pix="yuv420p", fps="30/1", audio=True,
                acodec="aac", sample_rate="48000", channels=2):
    streams = [{"codec_type": "video", "codec_name": vcodec, "width": width,
                "height": height, "pix_fmt": pix, "r_frame_rate": fps}]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": acodec,
                        "sample_rate": sample_rate, "channels": channels})
    return {"format": {"duration": str(duration)}, "streams": streams}


def _entries(*infos):
    return [{"path": f"C:/v/v{i}.mp4", "info": info} for i, info in enumerate(infos)]


class TestMergeCopyCompat:
    def test_identical_streams_can_copy(self):
        assert fu.can_merge_copy([_merge_info(), _merge_info()]) is True

    def test_different_codec_cannot_copy(self):
        assert fu.can_merge_copy(
            [_merge_info(), _merge_info(vcodec="hevc")]) is False

    def test_different_resolution_cannot_copy(self):
        assert fu.can_merge_copy(
            [_merge_info(), _merge_info(width=1280, height=720)]) is False

    def test_different_audio_cannot_copy(self):
        assert fu.can_merge_copy(
            [_merge_info(), _merge_info(acodec="ac3")]) is False

    def test_one_side_without_audio_cannot_copy(self):
        assert fu.can_merge_copy(
            [_merge_info(), _merge_info(audio=False)]) is False

    def test_needs_two_files(self):
        assert fu.can_merge_copy([_merge_info()]) is False
        assert fu.can_merge_copy([]) is False


class TestMergeAudioFallback:
    def test_copy_becomes_aac(self):
        codec, br, note = fu.merge_audio_fallback("mp4", "copy", "")
        assert codec == "aac" and br == "192k" and "AAC" in note

    def test_explicit_codec_kept(self):
        assert fu.merge_audio_fallback("mp4", "ac3", "256k") == ("ac3", "256k", "")

    def test_webm_uses_opus(self):
        codec, br, note = fu.merge_audio_fallback("webm", "copy", "")
        assert codec == "libopus" and br == "128k" and "Opus" in note


class TestBuildMergeCommand:
    def test_basic_filter_graph(self):
        entries = _entries(_merge_info(10), _merge_info(20))
        cmd, total, note = fu.build_merge_command(
            entries, "C:/out/merged.mp4", "software", "h264", 23, "", "medium", "")
        assert cmd is not None and note == ""
        assert total == pytest.approx(30.0)
        joined = " ".join(cmd)
        assert "concat=n=2:v=1:a=1[vcat][acat]" in joined
        assert "scale=1920:1080:force_original_aspect_ratio=decrease" in joined
        assert "fps=30/1" in joined
        assert "-map [vcat] -map [acat]" in joined
        assert "-c:v libx264" in joined and "-c:a aac" in joined

    def test_head_tail_trim_applied(self):
        entries = _entries(_merge_info(10), _merge_info(20))
        cmd, total, _ = fu.build_merge_command(
            entries, "C:/out/m.mp4", "software", "h264", 23, "", "medium", "",
            head_trim="00:00:02", tail_trim="00:00:03")
        joined = " ".join(cmd)
        assert total == pytest.approx((10 - 2 - 3) + (20 - 2 - 3))
        assert "trim=start=2.000:end=7.000" in joined
        assert "trim=start=2.000:end=17.000" in joined

    def test_silent_audio_added_for_missing_track(self):
        entries = _entries(_merge_info(10), _merge_info(10, audio=False))
        cmd, _, _ = fu.build_merge_command(
            entries, "C:/out/m.mp4", "software", "h264", 23, "", "medium", "")
        assert "anullsrc=r=48000:cl=stereo,atrim=0:10.000" in " ".join(cmd)

    def test_scale_with_auto_dimension(self):
        entries = _entries(_merge_info(10), _merge_info(10))
        cmd, _, _ = fu.build_merge_command(
            entries, "C:/out/m.mp4", "software", "h264", 23, "", "medium", "1280:-2")
        # 16:9 源，1280 宽 -> 高 720
        assert "scale=1280:720:force_original_aspect_ratio=decrease" in " ".join(cmd)

    def test_rejects_single_file(self):
        cmd, total, msg = fu.build_merge_command(
            _entries(_merge_info()), "C:/out/m.mp4", "software", "h264", 23, "", "medium", "")
        assert cmd is None and "2 个文件" in msg

    def test_rejects_copy_platform(self):
        cmd, _, msg = fu.build_merge_command(
            _entries(_merge_info(), _merge_info()), "C:/out/m.mp4", "copy", "h264", 23, "", "medium", "")
        assert cmd is None and "不支持" in msg

    def test_trim_longer_than_file_rejected(self):
        cmd, _, msg = fu.build_merge_command(
            _entries(_merge_info(10), _merge_info(10)), "C:/out/m.mp4",
            "software", "h264", 23, "", "medium", "",
            head_trim="00:00:08", tail_trim="00:00:08")
        assert cmd is None and "时长为 0" in msg

    def test_copy_command_uses_concat_demuxer(self):
        cmd = fu.build_merge_copy_command("C:/out/m.mp4", "C:/tmp/list.txt")
        joined = " ".join(cmd)
        assert "-f concat -safe 0 -i C:/tmp/list.txt" in joined
        assert "-c copy" in joined
        assert "-movflags +faststart" in joined
