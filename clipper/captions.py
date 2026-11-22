"""Word-level captions (faster-whisper) and the ASS subtitle file with hook + credit."""
import re

PROFANITY = [
    "fuck", "fucking", "fucked", "shit", "bitch", "dick", "pussy", "cunt",
    "nigga", "nigger", "fag", "faggot", "retard", "motherfucker",
]
YELLOW = "&H0000D7FF"   # ASS colours are BGR
WHITE = "&H00FFFFFF"

_model_cache = {}


def transcribe(path, model_size="small", language="en"):
    """Returns [{"start","end","word"}]. Empty list if speech can't be read.
    language "en" = English captions. Any other value (e.g. "hi", "ur") = speech in that
    language translated into English captions, so every clip has English captions."""
    from faster_whisper import WhisperModel  # heavy import, only when needed

    if model_size not in _model_cache:
        _model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    model = _model_cache[model_size]
    if language in (None, "en"):
        kw = {"language": "en", "task": "transcribe"}
    else:
        kw = {"language": None if language == "auto" else language, "task": "translate"}
    segments, _ = model.transcribe(
        path, word_timestamps=True, vad_filter=True, beam_size=1, **kw
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"start": float(w.start), "end": float(w.end), "word": text})
    return words


def censor(word):
    core = re.sub(r"[^a-z]", "", word.lower())
    for bad in PROFANITY:
        if core.startswith(bad):
            return word[0] + "*" * (len(word) - 1)
    return word


def chunk(words, max_words=3, max_gap=0.6):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > max_gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def ts(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def esc(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


LAYOUT_Y = {
    # (hook y, captions y, credit y)
    "blur": (390, 1330, 1790),
    "split": (700, 1480, 1850),
}


def build_ass(words, hook, credit, duration, layout="blur", censor_words=True,
              disclosure=None, font="Anton"):
    hook_y, cap_y, credit_y = LAYOUT_Y.get(layout, LAYOUT_Y["blur"])
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},112,{WHITE},{WHITE},&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1
Style: Hook,{font},84,&H00101010,&H00101010,{WHITE},&H00000000,0,0,0,0,100,100,0,0,3,18,0,5,90,90,0,1
Style: Credit,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,3,0,5,40,40,0,1
Style: Disc,{font},34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,9,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    end = ts(duration)
    ev = []
    if hook:
        ev.append(f"Dialogue: 2,{ts(0)},{end},Hook,,0,0,0,,{{\\pos(540,{hook_y})}}{esc(hook.upper())}")
    if credit:
        ev.append(f"Dialogue: 1,{ts(0)},{end},Credit,,0,0,0,,{{\\pos(540,{credit_y})\\alpha&H30&}}{esc(credit)}")
    if disclosure:
        ev.append(f"Dialogue: 1,{ts(0)},{ts(min(3.0, duration))},Disc,,0,0,0,,{esc(disclosure)}")

    for group in chunk(words):
        g_start, g_end = group[0]["start"], group[-1]["end"]
        texts = [esc(censor(w["word"]) if censor_words else w["word"]).upper() for w in group]
        for i, w in enumerate(group):
            s = g_start if i == 0 else w["start"]
            e = group[i + 1]["start"] if i + 1 < len(group) else max(g_end, s + 0.25)
            parts = []
            for j, t in enumerate(texts):
                parts.append(f"{{\\c{YELLOW}}}{t}{{\\c{WHITE}}}" if j == i else t)
            pop = "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)" if i == 0 else ""
            ev.append(
                f"Dialogue: 3,{ts(s)},{ts(min(e, duration))},Cap,,0,0,0,,"
                f"{{\\pos(540,{cap_y}){pop}}}{' '.join(parts)}"
            )
    return header + "\n".join(ev) + "\n"
