"""Run behind HTTPS with gunicorn; see HOSTED-OAUTH.md. Never enable debug."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import requests
from cryptography.fernet import Fernet
from flask import Flask, jsonify, redirect, request

app = Flask(__name__)
ROOT = "https://valtorbyte.ir/papa-tdl-auth"
SCOPE = "https://www.googleapis.com/auth/drive.file"
config = json.loads(Path(os.environ["PAPA_OAUTH_CLIENT_FILE"]).read_text())["web"]
cipher = Fernet(os.environ["PAPA_OAUTH_ENCRYPTION_KEY"].encode())
database = os.environ["PAPA_OAUTH_DATABASE"]


@contextmanager
def db():
    conn = sqlite3.connect(database, timeout=15)
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (ticket TEXT PRIMARY KEY, secret TEXT, state TEXT UNIQUE, verifier TEXT, expires INTEGER, result TEXT)")
    conn.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
    conn.commit()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def google(data):
    response = requests.post("https://oauth2.googleapis.com/token", data={
        **data, "client_id": config["client_id"], "client_secret": config["client_secret"]}, timeout=20)
    result = response.json()
    if response.status_code != 200:
        raise ValueError("Google rejected authorization")
    return result


@app.after_request
def headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


@app.errorhandler(Exception)
def failure(exc):
    # Do not echo exceptions, request URLs, authorization codes or tokens.
    return jsonify(error="Sign-in could not be completed. Please retry."), 400


@app.post("/start")
def start():
    secret = (request.get_json(silent=True) or {}).get("poll_secret")
    if not isinstance(secret, str) or len(secret) != 43:
        return jsonify(error="Invalid request"), 400
    ticket, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    with db() as conn:
        if conn.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 1000:
            return jsonify(error="Busy"), 503
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,NULL)",
                     (ticket, hashlib.sha256(secret.encode()).hexdigest(), state, verifier, int(time.time()) + 600))
    return jsonify(ticket=ticket)


@app.get("/login/<ticket>")
def login(ticket):
    import base64
    from urllib.parse import urlencode
    with db() as conn:
        row = conn.execute("SELECT state,verifier,result FROM sessions WHERE ticket=?", (ticket,)).fetchone()
    if not row or row[2] is not None:
        return "Sign-in expired. Start again in PaPa-TDL.", 410
    challenge = base64.urlsafe_b64encode(hashlib.sha256(row[1].encode()).digest()).rstrip(b"=").decode()
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": config["client_id"], "redirect_uri": ROOT + "/callback",
        "response_type": "code", "scope": SCOPE, "access_type": "offline",
        "prompt": "consent select_account", "state": row[0],
        "code_challenge": challenge, "code_challenge_method": "S256"}))


@app.get("/callback")
def callback():
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT ticket,verifier,result FROM sessions WHERE state=?", (request.args.get("state", ""),)).fetchone()
        if not row or row[2] is not None:
            return "Sign-in expired or already completed. Start again in PaPa-TDL.", 410
        result = {"status": "denied"}
        if request.args.get("code") and not request.args.get("error"):
            tokens = google({"grant_type": "authorization_code", "code": request.args["code"],
                             "redirect_uri": ROOT + "/callback", "code_verifier": row[1]})
            if SCOPE not in tokens.get("scope", "").split() or not tokens.get("refresh_token"):
                raise ValueError("Missing offline access")
            result = {"status": "complete", "access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"]}
        conn.execute("UPDATE sessions SET result=? WHERE ticket=?", (cipher.encrypt(json.dumps(result).encode()).decode(), row[0]))
    if result["status"] != "complete":
        return "Google sign-in cancelled. Return to PaPa-TDL to try again.", 400
    return "Google sign-in successful. Return to PaPa-TDL; your VPS will finish connecting automatically."


@app.post("/poll")
def poll():
    data = request.get_json(silent=True) or {}
    secret = data.get("poll_secret", "")
    if not isinstance(secret, str):
        return jsonify(error="Invalid request"), 400
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT secret,result FROM sessions WHERE ticket=?", (data.get("ticket", ""),)).fetchone()
        if not row or not secrets.compare_digest(row[0], hashlib.sha256(secret.encode()).hexdigest()):
            return jsonify(status="expired"), 410
        if row[1] is None:
            return jsonify(status="pending")
        result = json.loads(cipher.decrypt(row[1].encode()))
        conn.execute("DELETE FROM sessions WHERE ticket=?", (data["ticket"],))
    return jsonify(result)


@app.post("/refresh")
def refresh():
    token = (request.get_json(silent=True) or {}).get("refresh_token")
    if not isinstance(token, str) or not 1 <= len(token) <= 8192:
        return jsonify(error="Invalid request"), 400
    result = google({"grant_type": "refresh_token", "refresh_token": token})
    return jsonify(access_token=result["access_token"], expires_in=result["expires_in"])


app.config["MAX_CONTENT_LENGTH"] = 16384


@app.get("/health")
def health():
    with db() as conn:
        conn.execute("SELECT 1").fetchone()
    return jsonify(status="ready")
