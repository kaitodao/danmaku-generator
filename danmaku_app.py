#!/usr/bin/env python3
"""弾幕動画ジェネレーター - YouTube URL → コメント付き動画を自動生成"""

import streamlit as st
import subprocess
import json
import random
import re
import os
import tempfile
import uuid
from pathlib import Path

st.set_page_config(
    page_title="弾幕動画ジェネレーター",
    page_icon="💬",
    layout="centered",
)

# --- カスタムCSS（スマホ対応） ---
st.markdown("""
<style>
    .stApp { max-width: 800px; margin: 0 auto; }
    .big-title { font-size: 1.8rem; font-weight: bold; text-align: center; margin-bottom: 0.5rem; }
    .sub-title { font-size: 1rem; text-align: center; color: #888; margin-bottom: 2rem; }
    @media (max-width: 768px) {
        .big-title { font-size: 1.4rem; }
        .stTextInput input { font-size: 16px !important; }
    }
</style>
""", unsafe_allow_html=True)

# --- 定数 ---
OUTPUT_DIR = Path(__file__).parent / "出力"
OUTPUT_DIR.mkdir(exist_ok=True)

FONT_CANDIDATES = [
    # macOS
    ("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc", "Hiragino Sans"),
    # Linux (Streamlit Cloud - fonts-noto-cjk)
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK JP"),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK JP"),
]


def find_font():
    for path, name in FONT_CANDIDATES:
        if os.path.exists(path):
            return name
    return "Noto Sans CJK JP"


FONT_NAME = find_font()


# ===========================================================
# 1. yt-dlp でライブチャットと動画をダウンロード
# ===========================================================
def download_video_and_chat(url: str, work_dir: str, progress_placeholder):
    """YouTube動画とライブチャットをダウンロード。動画パスとチャットJSONパスを返す"""
    vid = uuid.uuid4().hex[:8]

    # --- ライブチャット取得 ---
    progress_placeholder.text("チャットデータを取得中...")
    chat_path = os.path.join(work_dir, f"chat_{vid}.live_chat.json")
    cmd_chat = [
        "yt-dlp",
        "--skip-download",
        "--write-subs",
        "--sub-lang", "live_chat",
        "-o", os.path.join(work_dir, f"chat_{vid}"),
        url,
    ]
    result = subprocess.run(cmd_chat, capture_output=True, text=True, timeout=300)
    if not os.path.exists(chat_path):
        # live_chatがない場合
        return None, None, "この動画にはライブチャットデータがありません。ライブ配信のアーカイブ動画を指定してください。"

    # --- 動画取得（720p以下） ---
    progress_placeholder.text("動画をダウンロード中（少し時間がかかります）...")
    video_path = os.path.join(work_dir, f"video_{vid}.mp4")
    cmd_video = [
        "yt-dlp",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", video_path,
        url,
    ]
    result = subprocess.run(cmd_video, capture_output=True, text=True, timeout=600)
    if not os.path.exists(video_path):
        return None, None, f"動画のダウンロードに失敗しました。\n{result.stderr[-500:]}"

    return video_path, chat_path, None


# ===========================================================
# 2. ライブチャットJSON → ASS弾幕ファイル変換
# ===========================================================
def chat_to_ass(
    chat_path: str,
    ass_path: str,
    start_seconds: int = 0,
    end_seconds: int = 0,
    play_w: int = 640,
    play_h: int = 360,
    font_size: int = 48,
    scroll_duration: float = 8.0,
    include_emoji: bool = False,
):
    """ライブチャットJSONをASS弾幕ファイルに変換"""
    line_height = font_size + 8
    top_margin = 40
    max_text_len = 80

    def fmt_time(s):
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        cs = int((s % 1) * 100)
        return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"

    # チャット読み込み
    messages = []
    with open(chat_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            actions_wrapper = data.get("replayChatItemAction", {})
            if not actions_wrapper:
                continue
            offset_ms = int(actions_wrapper.get("videoOffsetTimeMsec", 0))
            offset_s = offset_ms / 1000.0
            if start_seconds > 0 and offset_s < start_seconds:
                continue
            if end_seconds > 0 and offset_s > end_seconds:
                break
            for action in actions_wrapper.get("actions", []):
                chat_item = action.get("addChatItemAction", {}).get("item", {})
                renderer = (
                    chat_item.get("liveChatTextMessageRenderer")
                    or chat_item.get("liveChatPaidMessageRenderer")
                    or {}
                )
                if not renderer:
                    continue
                msg_runs = renderer.get("message", {}).get("runs", [])
                text = ""
                for run in msg_runs:
                    if "text" in run:
                        text += run["text"]
                    elif "emoji" in run and include_emoji:
                        shortcuts = run["emoji"].get("shortcuts", [])
                        if shortcuts:
                            text += shortcuts[0]
                text = text.strip().replace("\n", " ")
                if not include_emoji:
                    text = re.sub(r":[a-zA-Z0-9_]+:", "", text).strip()
                if text and len(text) < max_text_len:
                    is_superchat = "liveChatPaidMessageRenderer" in chat_item
                    adjusted_s = offset_s - start_seconds
                    messages.append((adjusted_s, text, is_superchat))

    if not messages:
        return 0

    # ASS生成
    num_lanes = max(1, (play_h - top_margin) // line_height)
    lane_free_at = [0.0] * num_lanes
    colors = [
        "&H00FFFFFF",
        "&H0000FF00",
        "&H0000FFFF",
        "&H00FF8800",
        "&H00FF00FF",
    ]

    ass_lines = []
    for offset_s, text, is_superchat in messages:
        best_lane = 0
        best_time = lane_free_at[0]
        for i in range(num_lanes):
            if lane_free_at[i] <= offset_s:
                best_lane = i
                break
            if lane_free_at[i] < best_time:
                best_time = lane_free_at[i]
                best_lane = i

        y = top_margin + best_lane * line_height
        lane_free_at[best_lane] = offset_s + scroll_duration * 0.5

        start = fmt_time(offset_s)
        end = fmt_time(offset_s + scroll_duration)

        if is_superchat:
            color = "\\c&H0000FFFF"
            style = f"{{\\move({play_w},{y},-{play_w // 2},{y}){color}\\b1}}"
        else:
            color = "\\c" + random.choice(colors)
            style = f"{{\\move({play_w},{y},-{play_w // 2},{y}){color}}}"

        safe_text = (
            text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        )
        ass_lines.append(
            f"Dialogue: 0,{start},{end},Danmaku,,0,0,0,,{style}{safe_text}"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(f"""[Script Info]
Title: Live Chat Danmaku
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,{FONT_NAME},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""")
        for l in ass_lines:
            f.write(l + "\n")

    return len(ass_lines)


# ===========================================================
# 3. FFmpegで弾幕を動画に焼き付け
# ===========================================================
FONT_FILE_MAP = {
    "ゴシック（Noto Sans CJK JP）": [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    ],
    "明朝（Noto Serif CJK JP）": [
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc",
    ],
    "じゆうちょうフォント": [
        "/usr/share/fonts/truetype/jiyucho/JiyuchoRegular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    ],
}


def find_font_file(font_name: str) -> str:
    candidates = FONT_FILE_MAP.get(font_name, [])
    for fp in candidates:
        if os.path.exists(fp):
            return fp
    # フォールバック
    for fp in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    ]:
        if os.path.exists(fp):
            return fp
    return ""


def burn_danmaku(video_path: str, ass_path: str, output_path: str, progress_placeholder,
                 start_seconds: int = 0, end_seconds: int = 0,
                 telop_text: str = "", telop_font_size: int = 36,
                 telop_color: str = "#FFFFFF", telop_stroke_color: str = "#000000",
                 telop_stroke_width: int = 3, telop_font: str = "ゴシック（Noto Sans CJK JP）"):
    """ASSファイルを動画にオーバーレイ（時間指定・テロップ対応）"""
    progress_placeholder.text("弾幕を動画に合成中（数分かかることがあります）...")
    cmd = ["ffmpeg", "-y"]
    if start_seconds > 0:
        cmd += ["-ss", str(start_seconds)]
    cmd += ["-i", video_path]
    if end_seconds > 0:
        duration = end_seconds - start_seconds
        cmd += ["-t", str(duration)]

    # フィルター組み立て
    filters = [f"ass={ass_path}"]
    if telop_text:
        fontfile = find_font_file(telop_font)
        safe_telop = telop_text.replace("'", "\\'").replace(":", "\\:")
        color = telop_color.replace("#", "0x")
        s_color = telop_stroke_color.replace("#", "0x")
        drawtext = (
            f"drawtext=text='{safe_telop}'"
            f":fontsize={telop_font_size}"
            f":fontcolor={color}"
            f":x=(w-text_w)/2:y=2"
            f":borderw={telop_stroke_width}:bordercolor={s_color}"
        )
        if fontfile:
            drawtext += f":fontfile='{fontfile}'"
        filters.append(drawtext)

    vf = ",".join(filters)
    cmd += [
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "26",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        "-threads", "0",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    return result.returncode == 0, result.stderr[-500:] if result.returncode != 0 else ""


# ===========================================================
# UI
# ===========================================================
st.markdown('<div class="big-title">弾幕動画ジェネレーター</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-title">YouTube URL を入力 → コメント付き動画を自動生成</div>',
    unsafe_allow_html=True,
)

url = st.text_input("YouTube URL", placeholder="https://www.youtube.com/watch?v=...")

with st.expander("詳細設定"):
    st.markdown("**時間指定**（`MM:SS` または `HH:MM:SS` 形式）")
    tcol1, tcol2 = st.columns(2)
    with tcol1:
        start_time_str = st.text_input("開始時間", value="00:00", placeholder="00:00")
    with tcol2:
        end_time_str = st.text_input("終了時間（空欄=最後まで）", value="", placeholder="30:00")

    st.markdown("**テロップ（画面上部に常時表示）**")
    telop_text = st.text_input("テロップ文字（空欄=なし）", value="", placeholder="例：チャンネル登録よろしく！")
    tcol3, tcol4 = st.columns(2)
    with tcol3:
        telop_font_size = st.slider("テロップ文字サイズ", 12, 80, 36, step=4)
    with tcol4:
        telop_font = st.selectbox("テロップフォント", [
            "ゴシック（Noto Sans CJK JP）",
            "明朝（Noto Serif CJK JP）",
            "じゆうちょうフォント",
        ], index=0)
    tcol5, tcol6 = st.columns(2)
    with tcol5:
        telop_color = st.color_picker("テロップ文字色", value="#FFFFFF")
    with tcol6:
        telop_stroke_color = st.color_picker("ストローク色", value="#000000")
    telop_stroke_width = st.slider("ストローク太さ", 0, 10, 3, step=1)

    st.markdown("**弾幕設定**")
    col1, col2 = st.columns(2)
    with col1:
        font_size = st.slider("弾幕文字サイズ", 12, 72, 48, step=4)
        scroll_speed = st.slider("スクロール速度（秒）", 4.0, 14.0, 8.0, step=0.5)
    with col2:
        include_emoji = st.checkbox("絵文字を含める", value=False)

    # --- フォントCSS対応マップ ---
    FONT_CSS_MAP = {
        "ゴシック（Noto Sans CJK JP）": "'Noto Sans CJK JP', 'Hiragino Sans', sans-serif",
        "明朝（Noto Serif CJK JP）": "'Noto Serif CJK JP', 'Hiragino Mincho ProN', serif",
        "じゆうちょうフォント": "'Jiyucho', 'Hiragino Sans', sans-serif",
    }
    telop_css_font = FONT_CSS_MAP.get(telop_font, "sans-serif")

    # --- ストロークCSS生成 ---
    sw = telop_stroke_width
    sc = telop_stroke_color
    stroke_shadow = ", ".join([
        f"{dx}px {dy}px 0 {sc}"
        for dx in range(-sw, sw + 1)
        for dy in range(-sw, sw + 1)
        if dx != 0 or dy != 0
    ]) if sw > 0 else "none"

    # --- 動画風プレビュー（16:9） ---
    st.markdown("**プレビュー（実際の動画イメージ）**")
    telop_html = ""
    if telop_text:
        telop_html = (
            f'<div style="position:absolute; top:0; left:0; right:0; text-align:center; z-index:2; padding:2px 0;">'
            f'<span style="font-size:{telop_font_size}px; color:{telop_color}; '
            f'font-family:{telop_css_font}; '
            f'text-shadow: {stroke_shadow};">'
            f'{telop_text}</span></div>'
        )
    st.markdown(
        f'<div style="position:relative; width:100%; aspect-ratio:16/9; background:#111; '
        f'border-radius:8px; overflow:hidden; border:1px solid #444;">'
        # テロップ
        f'{telop_html}'
        # 弾幕（上部）
        f'<div style="position:absolute; top:25%; left:10%; z-index:1;">'
        f'<span style="font-size:{font_size}px; color:#0f0; white-space:nowrap; '
        f'text-shadow: -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 1px 1px 0 #000;">'
        f'弾幕サンプルテキスト</span></div>'
        # 弾幕（中部）
        f'<div style="position:absolute; top:45%; right:5%; z-index:1;">'
        f'<span style="font-size:{font_size}px; color:#ff0; white-space:nowrap; '
        f'text-shadow: -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 1px 1px 0 #000;">'
        f'コメントサンプル</span></div>'
        # 弾幕（下部）
        f'<div style="position:absolute; top:65%; left:30%; z-index:1;">'
        f'<span style="font-size:{font_size}px; color:#f0f; white-space:nowrap; '
        f'text-shadow: -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 1px 1px 0 #000;">'
        f'wwwww</span></div>'
        # 中央の再生ボタン風
        f'<div style="position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); '
        f'font-size:48px; color:rgba(255,255,255,0.2);">▶</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def parse_time_str(t: str) -> int:
    """MM:SS or HH:MM:SS 形式を秒に変換。空文字は0を返す"""
    t = t.strip()
    if not t:
        return 0
    parts = t.split(":")
    parts = [int(p) for p in parts]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0

if st.button("弾幕動画を生成", type="primary", use_container_width=True):
    if not url:
        st.error("URLを入力してください")
    else:
        progress = st.empty()
        progress_bar = st.progress(0)

        with tempfile.TemporaryDirectory() as work_dir:
            # Step 1: ダウンロード
            progress_bar.progress(10)
            video_path, chat_path, err = download_video_and_chat(url, work_dir, progress)
            if err:
                st.error(err)
                st.stop()

            # Step 2: ASS変換
            progress_bar.progress(50)
            progress.text("弾幕データを変換中...")
            ass_path = os.path.join(work_dir, "danmaku.ass")
            start_sec = parse_time_str(start_time_str)
            end_sec = parse_time_str(end_time_str)
            count = chat_to_ass(
                chat_path,
                ass_path,
                start_seconds=start_sec,
                end_seconds=end_sec,
                font_size=font_size,
                scroll_duration=scroll_speed,
                include_emoji=include_emoji,
            )
            if count == 0:
                st.error("チャットメッセージが見つかりませんでした")
                st.stop()

            st.info(f"{count}件のコメントを弾幕に変換しました")

            # Step 3: FFmpegで合成
            progress_bar.progress(60)
            output_filename = f"danmaku_{uuid.uuid4().hex[:6]}.mp4"
            output_path = str(OUTPUT_DIR / output_filename)
            success, err_msg = burn_danmaku(video_path, ass_path, output_path, progress,
                                            start_seconds=start_sec, end_seconds=end_sec,
                                            telop_text=telop_text, telop_font_size=telop_font_size,
                                            telop_color=telop_color, telop_stroke_color=telop_stroke_color,
                                            telop_stroke_width=telop_stroke_width, telop_font=telop_font)

            if not success:
                st.error(f"動画の合成に失敗しました\n{err_msg}")
                st.stop()

            progress_bar.progress(100)
            progress.text("完成！")

            # 完成動画を表示
            st.success("弾幕動画が完成しました！")
            st.video(output_path)

            # ダウンロードボタン
            with open(output_path, "rb") as f:
                st.download_button(
                    label="動画をダウンロード",
                    data=f,
                    file_name=output_filename,
                    mime="video/mp4",
                    use_container_width=True,
                )

# --- フッター ---
st.markdown("---")
st.markdown(
    '<p style="text-align:center; color:#666; font-size:0.8rem;">'
    "弾幕動画ジェネレーター v1.0 | ライブ配信アーカイブ専用"
    "</p>",
    unsafe_allow_html=True,
)
