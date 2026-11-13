#!/usr/bin/env python3
"""
Tek veritabanı: GitHub'daki 'veri' dalı. Mac ve GitHub aynı veri.db'yi kullanır.

  al  <github|mac>  veri dalını çeker, kilit koyar, veri.db'yi çalışma klasörüne kopyalar
  ver <github|mac>  güncel veri.db'yi geri yazar ve kilidi kaldırır
  oku               kilit koymadan yalnızca okur (sayfa üretmek için)

Birbirini bozmama kuralları:
  * Kilit (kilit.json) varken ve 3 saatten yeniyse diğer taraf çalışmaz (çıkış kodu 3).
  * Her yazma "git push --force-with-lease" ile yapılır: veri dalı, okuduğumuz andan beri
    başkası tarafından değiştirildiyse üzerine yazılmaz; yerel veri yedeklenir.
  * Veri dalı her yazmada tek bir commit'ten oluşur, yani depo şişmez.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GIT_DIZIN = ROOT / ".veri_git"
DURUM = ROOT / ".senkron_durum.json"
MAC_AYAR = ROOT / ".mac_ayar.json"
TOKEN_DOSYA = ROOT / ".github_token"
DAL = "veri"
KILIT = "kilit.json"
KILIT_SAAT = 3
KILIT_ATLA = 3  # çıkış kodu: diğer taraf çalışıyor


class SenkronHatasi(Exception):
    pass


class Cakisma(SenkronHatasi):
    pass


def _simdi() -> datetime:
    return datetime.now(timezone.utc)


def _gizle(metin: str) -> str:
    return re.sub(r"https://[^@\s/]+@", "https://***@", metin or "")


def depo_adi() -> str:
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    return json.loads(MAC_AYAR.read_text(encoding="utf-8"))["repo"]


def remote_url() -> str:
    if os.environ.get("SENKRON_UZAK"):  # yalnızca test için
        return os.environ["SENKRON_UZAK"]
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return f"https://x-access-token:{os.environ['GITHUB_TOKEN']}@github.com/{os.environ['GITHUB_REPOSITORY']}.git"
    repo = depo_adi()
    token = TOKEN_DOSYA.read_text(encoding="utf-8").strip()
    return f"https://{repo.split('/')[0]}:{token}@github.com/{repo}.git"


def git(*args, dizin: Path = GIT_DIZIN, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=dizin, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SenkronHatasi(_gizle((r.stderr or r.stdout).strip()))
    return r


def depo_hazirla(dizin: Path = GIT_DIZIN) -> None:
    dizin.mkdir(parents=True, exist_ok=True)
    if not (dizin / ".git").exists():
        git("init", "-q", dizin=dizin)
    git("config", "user.name", "takip-bot", dizin=dizin)
    git("config", "user.email", "takip-bot@users.noreply.github.com", dizin=dizin)
    if git("remote", "get-url", "origin", dizin=dizin, check=False).returncode == 0:
        git("remote", "set-url", "origin", remote_url(), dizin=dizin)
    else:
        git("remote", "add", "origin", remote_url(), dizin=dizin)


def _calisma_alanini_bosalt() -> None:
    for p in GIT_DIZIN.iterdir():
        if p.name == ".git":
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()


def _cek() -> str | None:
    """Veri dalını çalışma alanına getirir; dal yoksa None."""
    r = git("fetch", "-q", "--depth=1", "origin", DAL, check=False)
    if r.returncode != 0:
        hata = (r.stderr or "").lower()
        if "couldn't find remote ref" in hata or "not found" in hata:
            _calisma_alanini_bosalt()
            return None
        raise SenkronHatasi(_gizle(r.stderr.strip()))
    sha = git("rev-parse", "FETCH_HEAD").stdout.strip()
    git("checkout", "-q", "-f", "--detach", sha)
    git("clean", "-qfdx")
    return sha


def _gonder(beklenen: str | None, mesaj: str) -> str:
    """Çalışma alanını tek commit olarak yazar; dal araya başkası girerek değiştiyse Cakisma."""
    git("branch", "-D", "gecici", check=False)
    git("checkout", "-q", "--orphan", "gecici")
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", mesaj)
    r = git("push", "-q", f"--force-with-lease={DAL}:{beklenen or ''}", "origin", f"HEAD:refs/heads/{DAL}",
            check=False)
    yeni = git("rev-parse", "HEAD").stdout.strip()
    git("checkout", "-q", "--detach", yeni)
    git("branch", "-D", "gecici", check=False)
    if r.returncode != 0:
        raise Cakisma(_gizle(r.stderr.strip()))
    return yeni


def kilit_oku() -> dict | None:
    p = GIT_DIZIN / KILIT
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def kilit_taze(k: dict) -> bool:
    try:
        return datetime.fromisoformat(k["baslangic"]) > _simdi() - timedelta(hours=KILIT_SAAT)
    except (KeyError, ValueError, TypeError):
        return False


def _tasinacak(sahip: str) -> tuple[str, ...]:
    # GitHub kendi tarayıcı çerezlerini (oturum.json) da taşır; Mac kendi profilini kullanır.
    return ("veri.db", "oturum.json") if sahip == "github" else ("veri.db",)


def al(sahip: str) -> int:
    depo_hazirla()
    for deneme in (1, 2):
        sha = _cek()
        k = kilit_oku()
        if k and kilit_taze(k) and k.get("sahip") != sahip:
            yer = "Mac" if k.get("sahip") == "mac" else "GitHub"
            print(f"Veritabanı şu an {yer} tarafından kullanılıyor (başlangıç {k.get('baslangic', '?')[:16]} UTC). "
                  f"Bu çalışma atlandı.")
            return KILIT_ATLA
        (GIT_DIZIN / KILIT).write_text(json.dumps(
            {"sahip": sahip, "baslangic": _simdi().isoformat(timespec="seconds"),
             "makine": platform.node()}, ensure_ascii=False), encoding="utf-8")
        try:
            yeni = _gonder(sha, f"kilit: {sahip}")
            break
        except Cakisma:
            if deneme == 2:
                raise
    for ad in _tasinacak(sahip):
        kaynak = GIT_DIZIN / ad
        if kaynak.exists():
            shutil.copy2(kaynak, ROOT / ad)
        elif ad == "veri.db" and (ROOT / ad).exists():
            print("Uyarı: veri dalında veri.db yok; yerel veri.db kullanılacak.")
    DURUM.write_text(json.dumps({"sahip": sahip, "sha": yeni}), encoding="utf-8")
    print(f"Veritabanı alındı ve kilitlendi ({sahip}).")
    return 0


def ver(sahip: str) -> int:
    if not DURUM.exists():
        print("Kilit bu çalışmada alınmamış; geri yazılacak bir şey yok.")
        return 0
    d = json.loads(DURUM.read_text(encoding="utf-8"))
    depo_hazirla()
    git("checkout", "-q", "-f", "--detach", d["sha"])
    git("clean", "-qfdx")
    for ad in _tasinacak(sahip):
        if (ROOT / ad).exists():
            shutil.copy2(ROOT / ad, GIT_DIZIN / ad)
    (GIT_DIZIN / KILIT).unlink(missing_ok=True)
    try:
        _gonder(d["sha"], f"veri: {sahip} {_simdi():%Y-%m-%d %H:%M} UTC")
    except Cakisma as ex:
        yedek = ROOT / f"veri_cakisma_{datetime.now():%Y%m%d_%H%M%S}.db"
        shutil.copy2(ROOT / "veri.db", yedek)
        print("HATA: Veri dalı bu çalışma sırasında başka biri tarafından değiştirilmiş; üzerine yazılmadı.\n"
              f"Bu çalışmanın verisi {yedek.name} olarak saklandı.\n{ex}")
        DURUM.unlink(missing_ok=True)
        return 1
    DURUM.unlink(missing_ok=True)
    print(f"Veritabanı geri yazıldı, kilit kaldırıldı ({sahip}).")
    return 0


def oku() -> int:
    depo_hazirla()
    if _cek() is None or not (GIT_DIZIN / "veri.db").exists():
        print("Veri dalında veri.db yok.")
        return 1
    shutil.copy2(GIT_DIZIN / "veri.db", ROOT / "veri.db")
    k = kilit_oku()
    if k and kilit_taze(k):
        print(f"Not: veritabanı şu an {k.get('sahip')} tarafından kullanılıyor; okunan kopya bir önceki hali.")
    return 0


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("al", "ver", "oku") or (
            sys.argv[1] in ("al", "ver") and (len(sys.argv) < 3 or sys.argv[2] not in ("github", "mac"))):
        print("Kullanım: senkron.py al|ver github|mac   ya da   senkron.py oku")
        sys.exit(2)
    try:
        kod = oku() if sys.argv[1] == "oku" else (al if sys.argv[1] == "al" else ver)(sys.argv[2])
    except SenkronHatasi as ex:
        print(f"Senkron hatası: {ex}")
        kod = 1
    sys.exit(kod)


if __name__ == "__main__":
    main()
