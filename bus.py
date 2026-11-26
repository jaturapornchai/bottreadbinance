"""Kafka (Redpanda) bus: the only way team members talk to each other."""
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

BOOT = os.environ.get("KAFKA_BOOTSTRAP", "127.0.0.1:19092")
TOPICS = ["bt.chart.request", "bt.chart.result",
          "bt.dca.request", "bt.dca.candidates", "bt.dca.review", "bt.dca.plan", "bt.team.chat"]
RETENTION_MS = str(30 * 86400 * 1000)
KAFKA_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "kafka.json")   # what the app's Kafka screen shows
log = logging.getLogger("bus")
_producer = None
stop = threading.Event()   # set when a one-shot run is finished: listeners leave their group cleanly and return
groups = {}                # group -> topics of every listener started in this process
working = set()            # groups busy with a batch or idle job right now (a run must not exit under them)
dropped = set()            # groups that gave up on a batch after 3 attempts (that link of the chain is dead)
start_at_end = False       # one-shot rounds skip any backlog: Redpanda forgets an idle group's offsets after 7 days
                           # and 'earliest' would then replay 30 days of news into the LLM


def ensure_topics():
    admin = AdminClient({"bootstrap.servers": BOOT})
    have = admin.list_topics(timeout=15).topics
    missing = [NewTopic(t, 1, 1, config={"retention.ms": RETENTION_MS}) for t in TOPICS if t not in have]
    for fut in (admin.create_topics(missing).values() if missing else []):
        fut.result()


def send(topic, msg):
    global _producer
    if _producer is None:
        _producer = Producer({"bootstrap.servers": BOOT, "linger.ms": 5})
    _producer.produce(topic, json.dumps(msg, ensure_ascii=False).encode("utf-8", "replace"))   # an AI's lone surrogate -> "?", never a raise
    _producer.poll(0)


def flush():
    if _producer:
        _producer.flush(15)


def listen(group, topics, handle, batch=1, idle=None):
    """Consume until `stop` is set. handle([(topic, msg)]) then commit (at-least-once).
    A batch that fails 3 times is skipped so one poison message can't jam the team."""
    c = Consumer({"bootstrap.servers": BOOT, "group.id": "bt-" + group,
                  "auto.offset.reset": "earliest", "enable.auto.commit": False,
                  "max.poll.interval.ms": 3_600_000})   # a handler may take minutes (the buyer's AI exit review); 5 min default = kicked out, then redelivered
    if start_at_end and group not in groups:   # first start only: a supervise() restart keeps its queue
        c.commit(offsets=[TopicPartition(t, 0, c.get_watermark_offsets(TopicPartition(t, 0), timeout=10)[1]) for t in topics],
                 asynchronous=False)
    c.subscribe(topics)
    groups[group] = topics
    try:
        while not stop.is_set():
            msgs = [m for m in c.consume(batch, 1.0) if not m.error()]
            working.add(group)
            try:
                if msgs:
                    decoded = [(m.topic(), json.loads(m.value())) for m in msgs]
                    for attempt in range(3):
                        try:
                            handle(decoded)
                            break
                        except Exception:  # noqa: BLE001
                            log.exception("%s: batch failed (attempt %d)", group, attempt + 1)
                            time.sleep(20)
                    else:
                        dropped.add(group)
                    c.commit(asynchronous=False)
                if idle:
                    idle()
            finally:
                working.discard(group)
    finally:
        c.close()


def lag(group, topics):
    """Messages the group has not committed yet (topics have 1 partition). Fetching committed offsets
    does not join the group, so this is safe while the real consumer runs."""
    c = Consumer({"bootstrap.servers": BOOT, "group.id": "bt-" + group, "enable.auto.commit": False})
    try:
        total = 0
        for tp in c.committed([TopicPartition(t, 0) for t in topics], timeout=10):
            lo, hi = c.get_watermark_offsets(tp, timeout=10)
            total += hi - (tp.offset if tp.offset >= 0 else lo)   # never committed -> starts at the earliest offset
        return total
    finally:
        c.close()


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _line(topic, m):
    raw = m.value().decode("utf-8", "replace")
    try:
        msg = json.loads(raw)
    except ValueError:
        msg = raw
    who = f"{msg['who']}: {msg['text']}" if isinstance(msg, dict) and {"who", "text"} <= msg.keys() else raw
    return {"topic": topic, "offset": m.offset(), "at": _iso(m.timestamp()[1]), "summary": who[:300],
            "json": json.dumps(msg, ensure_ascii=False, indent=1)[:20000]}


def tail(per_topic=100):
    """The last messages of every team topic, newest first, for the app's Kafka screen. assign() with no commit
    reads by offset: it never joins a team member's group or moves its place in the queue."""
    c = Consumer({"bootstrap.servers": BOOT, "group.id": "bt-log-viewer", "enable.auto.commit": False})
    topics, lines = [], []
    try:
        for t in TOPICS:
            lo, hi = c.get_watermark_offsets(TopicPartition(t, 0), timeout=10)
            pos = max(lo, hi - per_topic)
            topics.append({"topic": t, "count": hi - lo})
            if pos < hi:
                c.assign([TopicPartition(t, 0, pos)])
            while pos < hi:
                got = [m for m in c.consume(hi - pos, 10.0) if not m.error()]
                if not got:
                    break   # ponytail: a slow broker shows fewer lines this time, the next refresh gets them
                for m in got:
                    pos = m.offset() + 1
                    lines.append(_line(t, m))
    finally:
        c.close()
    lines.sort(key=lambda r: r["at"], reverse=True)
    return {"topics": topics, "messages": lines}


def save_tail():
    """`python bus.py tail` (the app runs it): cache/kafka.json, or just the error when Redpanda is down."""
    try:
        out = tail()
    except Exception as e:  # noqa: BLE001 — the app shows it
        out = {"error": f"อ่าน Kafka ไม่ได้: {str(e)[:200]}"}
    out["at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(KAFKA_LOG), exist_ok=True)
    tmp = f"{KAFKA_LOG}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, KAFKA_LOG)


if __name__ == "__main__" and sys.argv[1:] == ["tail"]:
    save_tail()
