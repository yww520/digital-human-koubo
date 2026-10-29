#!/usr/bin/env python3
"""小云雀 CLI 数字人视频生成调度器。

功能：
  1. 读取 script.json 与 persona.json
  2. 自动生成各段对口型驱动音频（优先 edge-tts，支持 macOS say 离线兜底）
  3. 调用 pippit-tool-cli generate-video 提交 Seedance 视频生成
  4. 轮询并下载生成片段至 gen/{sid}.mp4
  5. 吐出 ids.json，无缝对接后续 assemble.py / align.py / Remotion 剪辑工作流

用法：
  python3 xyq_generate.py script.json [--model Seedance_2.5] [--ratio 9:16] [--avatar assets/avatar_ref.jpg] [--quote-only] [--first-only]
"""

import os
import sys
import json
import time
import math
import shutil
import subprocess
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

def load_persona(custom_path=None):
    p_path = Path(custom_path) if custom_path else (HERE / "persona.json")
    if p_path.exists():
        return json.loads(p_path.read_text(encoding="utf-8"))
    return {}

def extract_json(text):
    if not text:
        return None
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e+1])
        except Exception:
            pass
    import re
    m_thread = re.search(r'"thread_id":\s*"([^"]+)"', text)
    m_run = re.search(r'"run_id":\s*"([^"]+)"', text)
    if m_thread and m_run:
        return {"thread_id": m_thread.group(1), "run_id": m_run.group(1)}
    return None

def check_xyq_cli():
    cli = shutil.which("pippit-tool-cli")
    if not cli:
        sys.exit("错误: 未找到 pippit-tool-cli 命令。请确认小云雀 CLI 已安装并添加到 PATH。")
    res = subprocess.run(["pippit-tool-cli", "status"], capture_output=True, text=True)
    if '"logged_in":true' not in (res.stdout + res.stderr):
        print("警告: pippit-tool-cli 尚未登录，请先运行 `pippit-tool-cli login`。")
    return True

def generate_tts_audio(sid, text, lang, cut_dir, tts_engine="auto", voice=None):
    cut_dir.mkdir(parents=True, exist_ok=True)
    target_audio = cut_dir / f"{sid}_voice.mp3"
    if target_audio.exists() and target_audio.stat().st_size > 1000:
        return target_audio

    # 1. 尝试 edge-tts
    if tts_engine in ("auto", "edge-tts") and shutil.which("edge-tts"):
        v = voice or ("zh-CN-XiaoxiaoNeural" if "zh" in lang.lower() or "mandarin" in lang.lower() else "en-US-JennyNeural")
        try:
            cmd = ["edge-tts", "--voice", v, "--text", text, "--write-media", str(target_audio)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and target_audio.exists():
                return target_audio
        except Exception as e:
            print(f"[{sid}] edge-tts 运行异常: {e}")

    # 2. 尝试 macOS say
    if sys.platform == "darwin" and shutil.which("say") and shutil.which("ffmpeg"):
        v_mac = voice or "Tingting"
        aiff_tmp = cut_dir / f"{sid}_temp.aiff"
        try:
            subprocess.run(["say", "-v", v_mac, "-o", str(aiff_tmp), text], check=True, capture_output=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff_tmp), "-ar", "44100", str(target_audio)], check=True)
            if aiff_tmp.exists():
                aiff_tmp.unlink()
            if target_audio.exists():
                return target_audio
        except Exception as e:
            print(f"[{sid}] say 转换异常: {e}")
            if aiff_tmp.exists():
                aiff_tmp.unlink()

    # 3. 检查是否已有同名音频 cut/{sid}.mp3 或 cut/{sid}.wav
    for alt in [cut_dir / f"{sid}.mp3", cut_dir / f"{sid}.wav"]:
        if alt.exists():
            return alt

    return None

def get_audio_duration(audio_path):
    if not audio_path or not Path(audio_path).exists():
        return None
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio_path)],
            capture_output=True, text=True, check=True
        )
        return float(res.stdout.strip())
    except Exception:
        return None

def main():
    parser = argparse.ArgumentParser(description="小云雀 CLI 批量生成数字人分段口播视频")
    parser.add_argument("script", help="剧本文件路径 (script.json)")
    parser.add_argument("--persona", default=None, help="人设配置文件 persona.json")
    parser.add_argument("--avatar", default=None, help="数字人参考图路径")
    parser.add_argument("--model", default=None, help="视频模型，如 Seedance_2.5, Seedance_2.0_mini_lite")
    parser.add_argument("--ratio", default=None, help="视频比例，默认 9:16")
    parser.add_argument("--resolution", default=None, help="分辨率，默认 720p")
    parser.add_argument("--tts", default="auto", choices=["auto", "edge-tts", "say", "none"], help="TTS 音频驱动模式")
    parser.add_argument("--voice", default=None, help="指定 TTS 音色名称")
    parser.add_argument("--out-dir", default="gen", help="生成的视频存储目录 (默认 gen/)")
    parser.add_argument("--cut-dir", default="cut", help="生成的中间音频与剪辑存储目录 (默认 cut/)")
    parser.add_argument("--ids-out", default="ids.json", help="输出的任务 ID 映射文件")
    parser.add_argument("--poll-interval", type=int, default=8, help="轮询小云雀任务的间隔秒数")
    parser.add_argument("--quote-only", action="store_true", help="仅打印段落分析、时长与报价估算，不实际发起生成")
    parser.add_argument("--first-only", action="store_true", help="仅生成第 1 段，用于快速确认音色和数字人口型效果")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认，直接执行")

    args = parser.parse_args()

    check_xyq_cli()
    persona = load_persona(args.persona)

    script_path = Path(args.script)
    if not script_path.exists():
        sys.exit(f"错误: 剧本文件不存在: {script_path}")

    segments = json.loads(script_path.read_text(encoding="utf-8"))
    if not isinstance(segments, list) or len(segments) == 0:
        sys.exit("错误: script.json 格式应为非空列表 [{id, t, shot, ...}]")

    xyq_cfg = persona.get("xyq", {})
    model = args.model or xyq_cfg.get("model") or "Seedance_2.5"
    ratio = args.ratio or xyq_cfg.get("ratio") or "9:16"
    resolution = args.resolution or xyq_cfg.get("resolution") or "720p"
    
    # 确定人像参考图
    avatar = args.avatar or xyq_cfg.get("avatar_asset") or persona.get("assets", {}).get("avatar") or "assets/avatar_ref.jpg"
    avatar_path = Path(avatar)
    if not avatar_path.is_absolute():
        avatar_path = (HERE / avatar_path).resolve()
        if not avatar_path.exists():
            avatar_path = (Path.cwd() / avatar).resolve()

    if not avatar_path.exists():
        print(f"⚠️ 提示: 参考图未找到 {avatar_path}，将仅以文字提示词生图")
        avatar_path = None

    cut_dir = Path(args.cut_dir)
    out_dir = Path(args.out_dir)
    cut_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("🎬 数字人口播 · 小云雀 CLI 驱动引擎")
    print(f"模型: {model} | 比例: {ratio} | 分辨率: {resolution}")
    print(f"人像参考图: {avatar_path if avatar_path else '无'}")
    print(f"总段数: {len(segments)} 段")
    print("=" * 60 + "\n")

    # Step 1: 预处理与 TTS 生成
    tasks_plan = []
    total_audio_sec = 0.0

    for s in segments:
        sid = s["id"]
        text = s["t"]
        shot = s.get("shot", "full")
        lang = s.get("lang", "Mandarin Chinese")
        
        # 生成或复用音频
        audio_file = None
        if args.tts != "none":
            audio_file = generate_tts_audio(sid, text, lang, cut_dir, tts_engine=args.tts, voice=args.voice)

        dur = get_audio_duration(audio_file)
        if dur:
            total_audio_sec += dur
            # Seedance duration 取整，最少 5 秒，常见档位 5s / 10s
            if dur <= 5.2:
                target_duration = 5
            elif dur <= 10.2:
                target_duration = 10
            else:
                target_duration = min(10, max(5, int(math.ceil(dur))))
                print(f"⚠️ 提示: [{sid}] 音频时长 {dur:.1f}s 超过 10 秒，建议将文案拆为短句以防口型偏移。")
        else:
            # 语速推算兜底
            char_len = len(text)
            est_dur = max(4, math.ceil(char_len / 5.4))
            target_duration = 5 if est_dur <= 5 else 10

        act_desc = persona.get("acts", {}).get(shot, persona.get("acts", {}).get("full", "面对镜头自然讲解"))
        prompt_tmpl = persona.get("xyq_prompt_template") or persona.get("prompt_template") or "参考图中的人物面向镜头自然口播，表情生动自然，对口型生动讲解，{act}。"
        prompt = prompt_tmpl.format(act=act_desc, line=text, ref="参考图", purpose="讲解知识", voice=persona.get("voice", {}).get("desc", ""))

        tasks_plan.append({
            "id": sid,
            "text": text,
            "shot": shot,
            "audio": str(audio_file) if audio_file else None,
            "duration": target_duration,
            "prompt": prompt
        })

    print(f"📊 任务清单就绪：共 {len(tasks_plan)} 段，音频口播总长约 {total_audio_sec:.1f} 秒。")
    for t in tasks_plan:
        audio_tag = f"音频: {Path(t['audio']).name}" if t['audio'] else "无音频(文生)"
        print(f"  • [{t['id']:4s}] 机位: {t['shot']:5s} | 设定时长: {t['duration']}s | {audio_tag} | 台词: {t['text'][:30]}...")

    if args.quote_only:
        print("\n[--quote-only 模式已完成报价，未发起实际生成]")
        return

    if not args.yes:
        confirm = input("\n确认开始调用小云雀 CLI 生成视频？[y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("已取消操作。")
            return

    # Step 2: 批量提交与并发轮询
    execution_list = tasks_plan[:1] if args.first_only else tasks_plan
    if args.first_only:
        print("\n🧪 [--first-only 模式已启用] 仅拍摄第 1 段供质检验收...")

    submitted = []
    for item in execution_list:
        sid = item["id"]
        print(f"\n🚀 正在提交 [{sid}] 到小云雀 CLI...")
        cmd = [
            "pippit-tool-cli", "generate-video",
            "--prompt", item["prompt"],
            "--model", model,
            "--duration", str(item["duration"]),
            "--ratio", ratio,
            "--resolution", resolution
        ]
        if avatar_path:
            cmd.extend(["--image", str(avatar_path)])
        if item["audio"]:
            cmd.extend(["--audio", str(Path(item["audio"]).resolve())])

        res = subprocess.run(cmd, capture_output=True, text=True)
        raw_out = res.stdout + res.stderr
        task_info = extract_json(raw_out)

        if not task_info or "thread_id" not in task_info or "run_id" not in task_info:
            print(f"❌ [{sid}] 提交失败: {raw_out}")
            continue

        thread_id = task_info["thread_id"]
        run_id = task_info["run_id"]
        web_link = task_info.get("web_thread_link")
        print(f"✓ [{sid}] 提交成功! Thread ID: {thread_id} | Run ID: {run_id}")
        if web_link:
            print(f"  🔗 在线预览: {web_link}")
        sys.stdout.flush()
        submitted.append({
            "sid": sid,
            "thread_id": thread_id,
            "run_id": run_id,
            "target_video": out_dir / f"{sid}.mp4"
        })

    print(f"\n⏳ 全部 {len(submitted)} 个片段任务已提交，开始并发轮询生成结果...")
    sys.stdout.flush()

    ids_map = {}
    pending = list(submitted)
    start_time = time.time()

    while pending and (time.time() - start_time < 900):  # 最多等待 15 分钟
        time.sleep(args.poll_interval)
        for item in list(pending):
            sid = item["sid"]
            thread_id = item["thread_id"]
            run_id = item["run_id"]
            target_video = item["target_video"]

            q_res = subprocess.run(
                ["pippit-tool-cli", "query-result", "--thread-id", thread_id, "--run-id", run_id, "--download-dir", str(out_dir)],
                capture_output=True, text=True
            )
            q_out = q_res.stdout + q_res.stderr
            q_data = extract_json(q_out)
            if q_data:
                if q_data.get("completed"):
                    videos = q_data.get("videos", [])
                    downloaded_path = None
                    if videos and isinstance(videos, list) and "output_path" in videos[0]:
                        downloaded_path = Path(videos[0]["output_path"])
                    if not downloaded_path or not downloaded_path.exists():
                        matches = list(out_dir.glob(f"*{thread_id}*.mp4"))
                        if matches:
                            downloaded_path = matches[0]
                        else:
                            mp4s = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
                            if mp4s:
                                downloaded_path = mp4s[0]

                    if downloaded_path and downloaded_path.exists():
                        if downloaded_path.resolve() != target_video.resolve():
                            shutil.copyfile(downloaded_path, target_video)
                        print(f"🎉 [{sid}] 视频生成并下载成功: {target_video}")
                        ids_map[sid] = thread_id
                        pending.remove(item)
                        sys.stdout.flush()

                elif q_data.get("error_message"):
                    print(f"❌ [{sid}] 小云雀返回错误: {q_data['error_message']}")
                    pending.remove(item)
                    sys.stdout.flush()

    if pending:
        for p in pending:
            print(f"⚠️ [{p['sid']}] 轮询超时，Thread ID: {p['thread_id']}")

    # 写入 ids.json
    ids_file = Path(args.ids_out)
    ids_file.write_text(json.dumps(ids_map, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 任务映射表已落盘: {ids_file}")
    print("\n👉 下一步（自动质检与剪辑）：")
    print(f"  python3 scripts/assemble.py {args.script} {args.ids_out} --out 成片_完整版.mp4")
    print(f"  python3 scripts/align.py {args.script} --out <工程目录>/src/tl.json")

if __name__ == "__main__":
    main()
