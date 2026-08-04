# memory.py — s09 跨会话记忆:存储、加载、提取、整理
import json, re
from .config import client, MODEL, WORKDIR

MEMORY_DIR = WORKDIR / ".memory"          # 每个工作区一份记忆(跨会话持久)
INDEX_FILE = MEMORY_DIR / "MEMORY.md"     # MEMORY.md 是索引，一行一个链接：
MAX_INJECT = 5
CONSOLIDATE_THRESHOLD = 10

def _slug(name):
    s = re.sub(r"[^a-z0-9\-]+", "-", name.lower().replace(" ", "-")).strip("-")
    # 纯中文等极端情况用 hash 后缀区分钟类记忆
    return s if s else f"memory-{abs(hash(name)) % 10000:04d}"

def list_memory_files():
    if not MEMORY_DIR.exists(): return []
    out = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md": continue
        raw = p.read_text(encoding="utf-8"); meta = {}
        #解析元数据
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end != -1:
                for line in raw[3:end].strip().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1); meta[k.strip()] = v.strip()
        out.append({"filename": p.name, "name": meta.get("name", p.stem),
                    "description": meta.get("description", ""),
                    "type": meta.get("type", "user"), "content": raw})
    return out

# 重新构建索引
def _rebuild_index():
    files = list_memory_files()
    INDEX_FILE.write_text("# Memory Index\n\n" +
        "\n".join(f"- [{f['name']}]({f['filename']}) — {f['description']}" for f in files) + "\n",
        encoding="utf-8")

# 写入新记忆，name、description、type是元数据，body是写入的memory内容
def write_memory_file(name, mem_type, description, body):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / f"{_slug(name)}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8")
    _rebuild_index()

# 给 build_system 注入 SYSTEM(记忆的元数据）
def read_memory_index() -> str:
    files = list_memory_files()
    return "\n".join(f"- {f['name']}: {f['description']}" for f in files) if files else ""

def _recent(messages, n=8):
    return "\n".join(f"{m.get('role')}: {str(m.get('content'))[:500]}"
                     for m in messages[-n:] if m.get("role") in ("user", "assistant"))

# ── 路二:side-query 选相关记忆,失败降级关键词 ──
def select_relevant_memories(messages, max_items=MAX_INJECT):
    files = list_memory_files()
    if not files: return []
    catalog = "\n".join(f"{i}: {f['name']} — {f['description']}" for i, f in enumerate(files))
    recent = _recent(messages)
    try:
        resp = client.chat.completions.create(model=MODEL, max_tokens=200, messages=[{"role":"user",
            "content": f"从记忆目录挑出与当前对话相关的条目,只返回索引的 JSON 数组,最多 {max_items} 个,"
                       f"不相关返回 []。\n\n当前对话:\n{recent}\n\n记忆目录:\n{catalog}"}])
        idxs = json.loads(re.search(r"\[.*?\]", resp.choices[0].message.content, re.S).group())
        return [files[i] for i in idxs[:max_items] if isinstance(i, int) and 0 <= i < len(files)]
    except Exception:
        low = recent.lower()
        return [f for f in files if any(w in low for w in (f["name"]+" "+f["description"]).lower().split())][:max_items]

# 每个用户轮开始调一次
def load_memories(messages):
    sel = select_relevant_memories(messages)
    if not sel: return
    block = "\n\n".join(f"## 记忆: {m['name']}\n{m['content']}" for m in sel)
    messages.append({"role": "user", "content": f"<relevant-memories>\n{block}\n</relevant-memories>"})
    print(f"\033[90m[memory] 注入 {len(sel)} 条相关记忆\033[0m")

# ── 提取(轮结束,从压缩前快照) ──  用LLM提取用户的偏好等，message构成为已有的记忆和最近的对话
def extract_memories(messages):
    existing = "\n".join(f"- {m['name']}: {m['description']}" for m in list_memory_files())
    prompt = ("从对话里提取值得长期记住的:用户偏好、约束、项目事实。"
              "返回 JSON 数组 [{name,type,description,body}],type∈{user,feedback,project,reference}。"
              "没有新信息或已被覆盖就返回 []。\n\n"
              f"已有记忆:\n{existing}\n\n对话:\n{_recent(messages, 10)[:4000]}")
    try:
        resp = client.chat.completions.create(model=MODEL, max_tokens=800,
                                              messages=[{"role":"user","content":prompt}])
        items = json.loads(re.search(r"\[.*\]", resp.choices[0].message.content, re.S).group())
        for it in items:
            if it.get("name") and it.get("body"):
                write_memory_file(it["name"], it.get("type","user"), it.get("description",""), it["body"])
        if items: print(f"\033[90m[memory] 新增 {len(items)} 条\033[0m")
    except Exception as e:
        print(f"\033[90m[memory] 提取跳过: {e}\033[0m")

# ── 整理(攒够阈值) ──
def consolidate_memories():
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD: return
    blob = "\n\n".join(f"[{f['type']}] {f['name']}: {f['description']}\n{f['content']}" for f in files)
    try:
        resp = client.chat.completions.create(model=MODEL, max_tokens=2000, messages=[{"role":"user",
            "content": "下面是积累的记忆,请去重、合并矛盾、删过时项,返回 JSON 数组 "
                       "[{name,type,description,body}],只留仍有用的。\n\n" + blob}])
        items = json.loads(re.search(r"\[.*\]", resp.choices[0].message.content, re.S).group())
        for p in MEMORY_DIR.glob("*.md"):
            if p.name != "MEMORY.md": p.unlink()
        for it in items:
            if it.get("name") and it.get("body"):
                write_memory_file(it["name"], it.get("type","user"), it.get("description",""), it["body"])
        print(f"\033[90m[memory] 整理 {len(files)}→{len(items)} 条\033[0m")
    except Exception as e:
        print(f"\033[90m[memory] 整理跳过: {e}\033[0m")