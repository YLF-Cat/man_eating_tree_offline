import os
import json
import random
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func


def now():
    return datetime.now()


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "offline-host-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

MAX_SID = 64
MIN_SID = 1


class Seed(db.Model):
    sid = db.Column(db.Integer, primary_key=True)  # 1..64
    r = db.Column(db.Integer, nullable=False)      # 0..89


class Student(db.Model):
    sid = db.Column(db.Integer, primary_key=True)  # 1..64
    score = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=now)


class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    preset_id = db.Column(db.String(64), nullable=True)
    content = db.Column(db.Text, nullable=False)
    options_json = db.Column(db.Text, nullable=False)  # JSON list[str]
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)
    settled_at = db.Column(db.DateTime, nullable=True)

    def options(self):
        try:
            options = json.loads(self.options_json)
            if isinstance(options, list):
                return [str(x) for x in options]
            return []
        except Exception:
            return []


class Answer(db.Model):
    __table_args__ = (
        db.UniqueConstraint("sid", "question_id", name="uq_answer_sid_question"),
    )

    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.Integer, nullable=False)  # 学号
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False)
    option_index = db.Column(db.Integer, nullable=False)  # 1..N
    code = db.Column(db.Integer, nullable=False)  # 学生交的X
    timestamp = db.Column(db.DateTime, default=now)
    score_change = db.Column(db.Float, default=0.0)

    question = db.relationship("Question", backref="answers")


def ensure_instance_dir():
    os.makedirs(app.instance_path, exist_ok=True)


def ensure_seeds():
    ensure_instance_dir()
    existing = {s.sid for s in Seed.query.all()}
    changed = False
    for sid in range(MIN_SID, MAX_SID + 1):
        if sid not in existing:
            r = random.randint(0, 89)  # 0..89 保证 R+i <= 99（i<=10）
            db.session.add(Seed(sid=sid, r=r))
            changed = True
    if changed:
        db.session.commit()


def load_presets():
    ensure_instance_dir()
    path = os.path.join(app.instance_path, "presets.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            presets = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                preset_id = str(item.get("id", "")).strip()
                content = str(item.get("content", "")).strip()
                options = item.get("options", [])
                if not preset_id or not content or not isinstance(options, list) or len(options) < 1:
                    continue
                options = [str(x) for x in options][:10]
                presets.append({"id": preset_id, "content": content, "options": options})
            return presets
    except Exception:
        return []
    return []


def get_active_question():
    return Question.query.filter_by(is_active=True).order_by(Question.created_at.desc()).first()


def decode_code(code_int: int, question: Question):
    options = question.options()
    n = len(options)

    sid = code_int // 100
    b = code_int % 100

    if sid < MIN_SID or sid > MAX_SID:
        raise ValueError(f"学号必须是 {MIN_SID}~{MAX_SID}")

    if b < 1 or b > 99:
        raise ValueError("密文后两位(B)必须是 01~99")

    seed = Seed.query.get(sid)
    if seed is None:
        raise ValueError("该学号未生成种子，请先检查种子表")

    option_index = b - seed.r
    if option_index < 1 or option_index > n:
        raise ValueError("密文无法解出有效选项（可能是算错/抄错/本题选项数变化）")

    return sid, option_index


def ensure_student_exists(sid: int):
    student = Student.query.get(sid)
    if student:
        return student

    min_score = db.session.query(func.min(Student.score)).scalar()
    if min_score is None:
        min_score = 0.0

    student = Student(sid=sid, score=float(min_score))
    db.session.add(student)
    db.session.commit()
    return student


def stats_for_question(question: Question):
    options = question.options()
    answers = Answer.query.filter_by(question_id=question.id).all()

    by_option = {i: [] for i in range(1, len(options) + 1)}
    answered = set()

    for a in answers:
        answered.add(a.sid)
        if a.option_index in by_option:
            by_option[a.option_index].append(a.sid)

    missing = [sid for sid in range(MIN_SID, MAX_SID + 1) if sid not in answered]
    return {
        "options": options,
        "by_option": by_option,
        "answered_count": len(answered),
        "missing": missing,
    }


@app.before_request
def _startup_once():
    try:
        ensure_seeds()
    except Exception:
        pass


@app.route("/")
def root():
    return redirect(url_for("operator"))


@app.route("/operator")
def operator():
    q = get_active_question()
    students = Student.query.order_by(Student.score.desc(), Student.sid.asc()).all()

    recent = []
    stats = None
    if q:
        recent = (
            Answer.query.filter_by(question_id=q.id)
            .order_by(Answer.timestamp.desc())
            .limit(12)
            .all()
        )
        stats = stats_for_question(q)

    return render_template(
        "operator.html",
        question=q,
        students=students,
        recent_answers=recent,
        stats=stats,
        max_sid=MAX_SID,
    )


@app.route("/operator/submit_code", methods=["POST"])
def submit_code():
    q = get_active_question()
    if not q:
        return jsonify({"ok": False, "error": "当前没有正在进行的题目，请先去“选题页”发布题目。"}), 400
    if q.settled_at is not None:
        return jsonify({"ok": False, "error": "本题已停止收集（已结算）。"}), 409

    payload = request.get_json(silent=True) or {}
    code_raw = str(payload.get("code", "")).strip()

    if not code_raw.isdigit():
        return jsonify({"ok": False, "error": "请输入纯数字 X"}), 400

    code_int = int(code_raw)

    try:
        sid, option_index = decode_code(code_int, q)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    ensure_student_exists(sid)

    existing = Answer.query.filter_by(question_id=q.id, sid=sid).first()
    if existing:
        return jsonify({"ok": False, "error": f"{sid} 号已提交过本题（如需更正，请先删除该学号本题提交）。"}), 409

    a = Answer(sid=sid, question_id=q.id, option_index=option_index, code=code_int)
    db.session.add(a)
    db.session.commit()

    stat = stats_for_question(q)
    option_text = stat["options"][option_index - 1] if 1 <= option_index <= len(stat["options"]) else ""

    return jsonify(
        {
            "ok": True,
            "sid": sid,
            "option_index": option_index,
            "option_text": option_text,
            "answered_count": stat["answered_count"],
            "missing_count": len(stat["missing"]),
            "timestamp": a.timestamp.strftime("%H:%M:%S"),
        }
    )


# 兼容：主页面“删除提交”仍用旧端点名 delete_submission
@app.route("/operator/delete_submission", methods=["POST"])
def delete_submission():
    q = get_active_question()
    if not q:
        flash("当前没有正在进行的题目")
        return redirect(url_for("operator"))

    sid_raw = request.form.get("sid", "").strip()
    if not sid_raw.isdigit():
        flash("学号必须是数字")
        return redirect(url_for("operator"))

    sid = int(sid_raw)
    a = Answer.query.filter_by(question_id=q.id, sid=sid).first()
    if not a:
        flash(f"未找到 {sid} 号在本题的提交")
        return redirect(url_for("operator"))

    db.session.delete(a)
    db.session.commit()
    flash(f"已删除 {sid} 号本题提交")
    return redirect(url_for("operator"))


@app.route("/question/<int:qid>/delete_submission", methods=["POST"])
def delete_submission_for_question(qid):
    q = Question.query.get_or_404(qid)

    sid_raw = request.form.get("sid", "").strip()
    next_page = request.form.get("next", "").strip()  # "operator" | "results"
    if not sid_raw.isdigit():
        flash("学号必须是数字")
        return redirect(url_for("results", qid=q.id) if next_page == "results" else url_for("operator"))

    sid = int(sid_raw)
    a = Answer.query.filter_by(question_id=q.id, sid=sid).first()
    if not a:
        flash(f"未找到 {sid} 号在本题的提交")
        return redirect(url_for("results", qid=q.id) if next_page == "results" else url_for("operator"))

    db.session.delete(a)
    db.session.commit()
    flash(f"已删除 {sid} 号本题提交")
    return redirect(url_for("results", qid=q.id) if next_page == "results" else url_for("operator"))


@app.route("/question/<int:qid>/edit_options", methods=["POST"])
def edit_options_for_question(qid):
    q = Question.query.get_or_404(qid)

    old_options = q.options()
    old_n = len(old_options)
    if old_n < 1:
        flash("题目选项异常，无法编辑")
        return redirect(url_for("operator"))

    new_options = []
    for i, old_text in enumerate(old_options, start=1):
        t = (request.form.get(f"opt{i}", "") or "").strip()
        new_options.append(t if t else old_text)

    append_raw = request.form.get("append_options", "") or ""
    append_lines = [line.strip() for line in append_raw.splitlines() if line.strip()]
    if append_lines:
        remaining = 10 - len(new_options)
        if remaining > 0:
            new_options.extend(append_lines[:remaining])

    # 不允许减少选项数量（防止 i 编号被破坏）
    if len(new_options) < old_n:
        flash("不允许减少选项数量（只能改文字或追加到末尾）")
        return redirect(url_for("operator"))

    # 保护：新长度不能小于已提交最大 option_index
    max_used = db.session.query(func.max(Answer.option_index)).filter_by(question_id=q.id).scalar()
    max_used = int(max_used or 0)
    if max_used > len(new_options):
        flash(f"选项数量不能小于已提交的最大选项编号：{max_used}")
        return redirect(url_for("operator"))

    q.options_json = json.dumps(new_options, ensure_ascii=False)
    db.session.commit()
    flash("已更新选项内容（编号不变）")

    next_page = (request.form.get("next", "") or "").strip()
    if next_page == "results":
        return redirect(url_for("results", qid=q.id))
    return redirect(url_for("operator"))


@app.route("/operator/settle", methods=["POST"])
def settle_question():
    q = get_active_question()
    if not q:
        flash("当前没有正在进行的题目")
        return redirect(url_for("operator"))

    q.settled_at = now()
    db.session.commit()
    return redirect(url_for("results", qid=q.id))


@app.route("/operator/score", methods=["POST"])
def score_actions():
    qid_raw = request.form.get("question_id", "").strip()
    next_page = request.form.get("next", "").strip()  # "operator" | "results"

    def go_back(qid=None):
        if next_page == "results" and qid is not None:
            return redirect(url_for("results", qid=qid))
        return redirect(url_for("operator"))

    if not qid_raw.isdigit():
        flash("缺少题目ID")
        return go_back()

    qid = int(qid_raw)
    q = Question.query.get(qid)
    if not q:
        flash("题目不存在")
        return go_back()

    action_type = request.form.get("action_type", "").strip()
    options = q.options()
    n = len(options)

    def parse_float(name):
        raw = request.form.get(name, "").strip()
        try:
            return float(raw)
        except Exception:
            return 0.0

    if action_type == "A":
        option_index = int(request.form.get("option_index", "0") or 0)
        change = parse_float("change")
        if option_index < 1 or option_index > n:
            flash("选项编号无效")
            return go_back(qid=q.id)

        answers = Answer.query.filter_by(question_id=q.id, option_index=option_index).all()
        for a in answers:
            s = ensure_student_exists(a.sid)
            s.score += change
            a.score_change += change
        db.session.commit()
        flash(f"已对本题选项 {option_index} 加分 {change}")
        return go_back(qid=q.id)

    if action_type == "B":
        sid = int(request.form.get("sid", "0") or 0)
        change = parse_float("change")
        if sid < MIN_SID or sid > MAX_SID:
            flash("学号无效")
            return go_back(qid=q.id)

        s = ensure_student_exists(sid)
        s.score += change

        a = Answer.query.filter_by(question_id=q.id, sid=sid).first()
        if a:
            a.score_change += change

        db.session.commit()
        flash(f"已对 {sid} 号 加分 {change}")
        return go_back(qid=q.id)

    if action_type == "C":
        option_index = int(request.form.get("option_index", "0") or 0)
        prob_percent = parse_float("prob_percent")
        prob = max(0.0, min(1.0, prob_percent / 100.0))
        change1 = parse_float("change1")
        change2 = parse_float("change2")
        if option_index < 1 or option_index > n:
            flash("选项编号无效")
            return go_back(qid=q.id)

        answers = Answer.query.filter_by(question_id=q.id, option_index=option_index).all()
        for a in answers:
            s = ensure_student_exists(a.sid)
            if random.random() < prob:
                s.score += change1
                a.score_change += change1
            else:
                s.score += change2
                a.score_change += change2

        db.session.commit()
        flash(f"已对本题选项 {option_index} 概率加分（{prob_percent}%）")
        return go_back(qid=q.id)

    flash("未知操作")
    return go_back(qid=q.id)


@app.route("/results/<int:qid>")
def results(qid):
    q = Question.query.get_or_404(qid)
    students = Student.query.order_by(Student.score.desc(), Student.sid.asc()).all()

    stat = stats_for_question(q)

    answers = Answer.query.filter_by(question_id=q.id).all()
    grouped = {i: [] for i in range(1, len(stat["options"]) + 1)}
    for a in answers:
        if a.option_index in grouped:
            grouped[a.option_index].append(a)

    by_option = []
    for i, text in enumerate(stat["options"], start=1):
        ans = sorted(grouped.get(i, []), key=lambda x: x.sid)
        by_option.append({"index": i, "text": text, "count": len(ans), "answers": ans})

    return render_template(
        "results.html",
        question=q,
        by_option=by_option,
        missing=stat["missing"],
        answered_count=stat["answered_count"],
        students=students,
        max_sid=MAX_SID,
    )


@app.route("/history")
def history():
    questions = Question.query.order_by(Question.created_at.desc()).all()
    return render_template("history.html", questions=questions)


@app.route("/presets")
def presets():
    presets_list = load_presets()
    active = get_active_question()
    return render_template("presets.html", presets=presets_list, active_question=active)


@app.route("/presets/start", methods=["POST"])
def start_preset():
    preset_id = request.form.get("preset_id", "").strip()
    presets_list = load_presets()
    preset = next((p for p in presets_list if p["id"] == preset_id), None)
    if not preset:
        flash("预设题目不存在，请检查 presets.json")
        return redirect(url_for("presets"))

    Question.query.filter_by(is_active=True).update({"is_active": False})
    q = Question(
        preset_id=preset["id"],
        content=preset["content"],
        options_json=json.dumps(preset["options"], ensure_ascii=False),
        is_active=True,
        created_at=now(),
        settled_at=None,
    )
    db.session.add(q)
    db.session.commit()

    flash(f"已发布题目：{preset['id']}")
    return redirect(url_for("operator"))


@app.route("/seeds")
def seeds():
    ensure_seeds()
    seeds_list = Seed.query.order_by(Seed.sid.asc()).all()
    return render_template("seeds.html", seeds=seeds_list, max_sid=MAX_SID)


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        ensure_seeds()
    app.run(host="0.0.0.0", port=1145, debug=True)
