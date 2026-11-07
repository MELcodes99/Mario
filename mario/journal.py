"""Trade journal & state (directive §11). SQLite, stdlib only.

Every candidate decision is recorded - including every rejection - plus scan
calls, trades, smart-wallet signals, AI raw responses and risk state.
"""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts REAL, mode TEXT, mint TEXT, symbol TEXT, age_min REAL,
  liquidity REAL, market_cap REAL, security_pass INTEGER, security_json TEXT,
  score REAL, score_json TEXT, strategies TEXT, decision TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_dec_mint ON decisions(mint);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, day TEXT, ts REAL, mint TEXT, symbol TEXT, name TEXT, strategy TEXT,
  score REAL, call_price REAL, call_mc REAL, call_liq REAL,
  last_price REAL, last_mc REAL, peak_price REAL, peak_mc REAL, peak_ts REAL, low_price REAL,
  last_update REAL, status TEXT, UNIQUE(day, mint));
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, mode TEXT, session_id TEXT, mint TEXT, symbol TEXT, strategy TEXT, creator TEXT,
  opened_ts REAL, closed_ts REAL, sol_in REAL, tokens_total INTEGER, tokens_left INTEGER,
  decimals INTEGER, realized_sol REAL, fees_sol REAL, stop_ratio REAL, peak_ratio REAL,
  tp_hits TEXT, entry_liq REAL, entry_mc REAL, score REAL, status TEXT, exit_reason TEXT,
  mae REAL, mfe REAL, entry_reason TEXT, slippage_json TEXT);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY, position_id INTEGER, ts REAL, side TEXT, sol REAL, tokens INTEGER,
  price_impact REAL, signature TEXT, reason TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS smart_wallets (
  address TEXT PRIMARY KEY, label TEXT, source TEXT, added_ts REAL, active INTEGER DEFAULT 1,
  signals INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
  quick_flips INTEGER DEFAULT 0, sum_return REAL DEFAULT 0, last_sig TEXT);
CREATE TABLE IF NOT EXISTS wallet_signals (
  id INTEGER PRIMARY KEY, ts REAL, wallet TEXT, mint TEXT, side TEXT, sol REAL, price REAL,
  evaluated INTEGER DEFAULT 0, outcome REAL);
CREATE INDEX IF NOT EXISTS ix_ws_mint ON wallet_signals(mint, ts);
CREATE TABLE IF NOT EXISTS ai_responses (id INTEGER PRIMARY KEY, ts REAL, mint TEXT, valid INTEGER, raw TEXT);
CREATE TABLE IF NOT EXISTS blacklist (mint_or_wallet TEXT PRIMARY KEY, kind TEXT, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Journal:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---- generic
    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, args=()) -> int:
        return self.db.execute(sql, args).lastrowid

    def get(self, k: str, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def put(self, k: str, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # ---- decisions
    def decision(self, mode, pair, sec, score, strategies, decision, reason):
        self.x("""INSERT INTO decisions(ts,mode,mint,symbol,age_min,liquidity,market_cap,security_pass,
                  security_json,score,score_json,strategies,decision,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (time.time(), mode, pair.mint if pair else None, pair.symbol if pair else None,
                pair.age_min if pair else None, pair.liquidity_usd if pair else None, pair.mc if pair else None,
                int(bool(sec and sec.passed)), json.dumps(sec.as_dict() if sec else None),
                score.total if score else None, json.dumps(score.parts if score else None),
                ",".join(strategies or []), decision, reason))

    # ---- blacklist
    def blacklisted(self, key: str) -> bool:
        return bool(self.q("SELECT 1 FROM blacklist WHERE mint_or_wallet=?", (key,)))

    def blacklist(self, key: str, kind: str, reason: str):
        self.x("INSERT OR REPLACE INTO blacklist VALUES(?,?,?,?)", (key, kind, reason, time.time()))

    def ai(self, mint, valid, raw):
        self.x("INSERT INTO ai_responses(ts,mint,valid,raw) VALUES(?,?,?,?)", (time.time(), mint, int(valid), raw))
