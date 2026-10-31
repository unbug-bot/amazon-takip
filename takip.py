#!/usr/bin/env python3
"""
GitHub Actions'ta çalışan yavaş Amazon.com.tr takibi.

Her çalıştırma küçük bir sayfa bütçesi (varsayılan 8) kullanır ve işleri sırayla yapar:
  kontrol : takipteki beden varyantlarının fiyat/stok kontrolü (en eskisi önce)
  arama   : arama sayfaları (Dior fiyatları buradan güncellenir; pantolon/gömlek için aday toplar)
  kesif   : aday ürün sayfasını açıp aranan bedeni arar
Ayrıştırma ve kayıt mantığı tracker.py'den gelir. Sonunda site/index.html üretilir.
"""
from __future__ import annotations

import html
import os
import random
import time
from datetime import datetime, timedelta

import tracker as T
from tracker import log, tl

OTURUM = T.ROOT / "oturum.json"
SITE = T.ROOT / "site"

ONLINE_VARSAYILAN = {
    "sayfa_butcesi": 8,
    "bekleme_saniye": [20, 40],
    "kontrol_saat": 4,
    "arama_saat_bedensiz": 6,
    "arama_saat_bedenli": 24,
}

EK_SCHEMA = """
CREATE TABLE IF NOT EXISTS durum(anahtar TEXT PRIMARY KEY, deger TEXT);
CREATE TABLE IF NOT EXISTS adaylar(asin TEXT PRIMARY KEY, takip_id TEXT, baslik TEXT, eklenme TEXT);
CREATE TABLE IF NOT EXISTS calismalar(ts TEXT, sayfa INTEGER, captcha INTEGER, sonuc TEXT);
"""


def simdi_s() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dget(con, k):
    r = con.execute("SELECT deger FROM durum WHERE anahtar=?", (k,)).fetchone()
    return r[0] if r else None


def dset(con, k, v) -> None:
    con.execute("INSERT OR REPLACE INTO durum VALUES(?,?)", (k, str(v)))
    con.commit()


def eski_mi(ts, saat) -> bool:
    return ts is None or datetime.fromisoformat(ts) < datetime.now() - timedelta(hours=saat)


# ---------------------------------------------------------------- tarayıcı

class OnlineTarayici:
    """tracker.Tarayici ile aynı arayüz (git, page). Oturum çerezleri dosyada saklanır."""

    def __init__(self, o: dict):
        from playwright.sync_api import sync_playwright
        self.o = o
        self.butce = int(o["sayfa_butcesi"])
        self.istek = 0
        self.captcha = 0
        self.pw = sync_playwright().start()
        self.tarayici = self.pw.chromium.launch(headless=True)
        kw = dict(locale="tr-TR", timezone_id="Europe/Istanbul",
                  viewport={"width": 1366, "height": 900})
        if OTURUM.exists():
            kw["storage_state"] = str(OTURUM)
        self.ctx = self.tarayici.new_context(**kw)
        self.page = self.ctx.new_page()

    def kalan(self) -> int:
        return self.butce - self.istek

    def _yukle(self, url: str) -> None:
        if self.istek >= self.butce + 6:  # keşif birkaç sayfa taşabilir, fazlası değil
            raise T.LimitAsildi()
        if self.istek:
            time.sleep(random.uniform(*self.o["bekleme_saniye"]))
        self.istek += 1
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def _captcha_mi(self) -> bool:
        return bool(self.page.query_selector('form[action*="validateCaptcha"]')) \
            or "captcha" in self.page.url.lower()

    def git(self, url: str) -> None:
        self._yukle(url)
        if self._captcha_mi():
            self.captcha += 1
            log("  CAPTCHA; ana sayfa açılıp bir kez yeniden denenecek")
            self._yukle(T.BASE)
            self._yukle(url)
            if self._captcha_mi():
                self.captcha += 1
                raise T.DogrulamaGerekli(url)
        try:
            btn = self.page.query_selector("#sp-cc-accept")
            if btn:
                btn.click(timeout=3000)
        except Exception:
            pass

    def kapat(self) -> None:
        for f in (lambda: self.ctx.storage_state(path=str(OTURUM)),
                  self.ctx.close, self.tarayici.close, self.pw.stop):
            try:
                f()
            except Exception:
                pass


# ---------------------------------------------------------------- görevler

def takip_bul(cfg, tid):
    return next((t for t in cfg["takipler"] if t["id"] == tid and t.get("aktif", True)), None)


def gorev_kontrol(tr, con, cfg, a, o, olaylar) -> bool:
    ids = [t["id"] for t in cfg["takipler"] if t.get("beden") and t.get("aktif", True)]
    if not ids:
        return False
    sinir = (datetime.now() - timedelta(hours=o["kontrol_saat"])).isoformat(timespec="seconds")
    r = con.execute(
        f"SELECT asin,takip_id,parent_asin,beden FROM urunler WHERE aktif=1 AND takip_id IN "
        f"({','.join('?' * len(ids))}) AND (son_kontrol IS NULL OR son_kontrol < ?) "
        f"ORDER BY son_kontrol LIMIT 1", (*ids, sinir)).fetchone()
    if not r:
        return False
    takip = takip_bul(cfg, r["takip_id"])
    b = T.guvenli(T.urun_sayfasi, tr, r["asin"])
    if b:
        dset(con, f"hata:{r['asin']}", 0)
        T.kaydet(con, takip, a, olaylar, asin=r["asin"], parent=r["parent_asin"], baslik=b["baslik"],
                 beden=r["beden"], fiyat=b["fiyat"], liste=b["liste"], stokta=b["stokta"])
        log(f"  kontrol: {b['baslik'][:45]} [{r['beden']}] {tl(b['fiyat'])} "
            f"{'stokta' if b['stokta'] else 'stok yok'}")
    else:
        hata = int(dget(con, f"hata:{r['asin']}") or 0) + 1
        dset(con, f"hata:{r['asin']}", hata)
        con.execute("UPDATE urunler SET son_kontrol=? WHERE asin=?", (simdi_s(), r["asin"]))
        if hata >= 5:
            con.execute("UPDATE urunler SET aktif=0 WHERE asin=?", (r["asin"],))
            log(f"  {r['asin']} 5 kez okunamadı, takipten çıkarıldı")
        con.commit()
    return True


def gorev_arama(tr, con, cfg, a, o, olaylar) -> bool:
    for t in cfg["takipler"]:
        if not t.get("aktif", True):
            continue
        saat = o["arama_saat_bedenli"] if t.get("beden") else o["arama_saat_bedensiz"]
        n = int(t.get("sayfa_sayisi", 3))
        for sorgu in t["aramalar"]:
            for s in range(1, n + 1):
                k = f"arama:{t['id']}:{sorgu}:{s}"
                if eski_mi(dget(con, k), saat):
                    _arama_yap(tr, con, t, a, olaylar, sorgu, s, n, k, saat)
                    return True
    return False


def _arama_yap(tr, con, t, a, olaylar, sorgu, s, n, k, saat) -> None:
    marka = bool(t.get("markalar")) and dget(con, f"markasiz:{t['id']}") is None
    sonuc = T._ara(tr, t, sorgu, s, marka)
    if not sonuc and s == 1 and marka:
        log("  marka filtresiyle sonuç yok; bu takipte filtresiz aranacak")
        dset(con, f"markasiz:{t['id']}", 1)
        sonuc = T._ara(tr, t, sorgu, s, False)
    dset(con, k, simdi_s())
    if not sonuc:
        try:
            sayfa_basligi = tr.page.title()[:60]
        except Exception:
            sayfa_basligi = "?"
        if s == 1:
            # İlk sayfanın boş gelmesi engelleme belirtisi olabilir: 24 saat değil 2 saat sonra yeniden dene
            ertele = (datetime.now() - timedelta(hours=max(saat - 2, 0))).isoformat(timespec="seconds")
            for s2 in range(1, n + 1):  # sonraki sayfalar da ilk sayfayla birlikte denensin
                dset(con, f"arama:{t['id']}:{sorgu}:{s2}", ertele)
            log(f"  arama '{sorgu}' s1: sonuç yok (sayfa: {sayfa_basligi}); 2 saat sonra yeniden denenecek")
            return
        for s2 in range(s + 1, n + 1):  # sonraki sayfalar da boştur
            dset(con, f"arama:{t['id']}:{sorgu}:{s2}", simdi_s())
        log(f"  arama '{sorgu}' s{s}: sonuç yok (sayfa: {sayfa_basligi})")
        return
    if t.get("beden"):
        gun = int(t.get("yeniden_tarama_gun", 7))
        eklenen = 0
        for r in sonuc:
            if T.baslik_uygun(r["baslik"], t) and not T.yakin_zamanda_tarandi(con, r["asin"], gun):
                eklenen += con.execute("INSERT OR IGNORE INTO adaylar VALUES(?,?,?,?)",
                                       (r["asin"], t["id"], r["baslik"], simdi_s())).rowcount
        con.commit()
        log(f"  arama '{sorgu}' s{s}: {len(sonuc)} sonuç, {eklenen} yeni aday")
    else:
        kayit = 0
        for r in sonuc:
            if not T.baslik_uygun(r["baslik"], t):
                continue
            if con.execute("SELECT 1 FROM urunler WHERE asin=? AND aktif=0", (r["asin"],)).fetchone():
                continue
            fiyat = T.parse_price(r["fiyat"])
            if fiyat is None:
                continue
            etiket = T.parfum_etiketi(r["baslik"], t["parfum"]) if t.get("parfum") else None
            T.kaydet(con, t, a, olaylar, asin=r["asin"], parent=None, baslik=r["baslik"],
                     beden=etiket or None, fiyat=fiyat, liste=T.parse_price(r["liste"]), stokta=True)
            kayit += 1
        log(f"  arama '{sorgu}' s{s}: {len(sonuc)} sonuç, {kayit} fiyat kaydedildi")


def gorev_kesif(tr, con, cfg, a, o, olaylar) -> bool:
    while True:
        r = con.execute("SELECT asin,takip_id,baslik FROM adaylar ORDER BY eklenme LIMIT 1").fetchone()
        if not r:
            return False
        con.execute("DELETE FROM adaylar WHERE asin=?", (r["asin"],))
        con.commit()
        t = takip_bul(cfg, r["takip_id"])
        if t and t.get("beden") and not T.yakin_zamanda_tarandi(
                con, r["asin"], int(t.get("yeniden_tarama_gun", 7))):
            break
    log(f"  keşif: {r['baslik'][:60]}")
    T.guvenli(T._kesfet, tr, con, t, a, olaylar, r["asin"], t["beden"],
              int(t.get("yeniden_tarama_gun", 7)))
    return True


GOREVLER = {"kontrol": gorev_kontrol, "arama": gorev_arama, "kesif": gorev_kesif}
SIRA = ["kontrol", "kontrol", "arama", "kesif"]
SIRA_KESIF = ["arama", "kesif"]  # elle "kesif" modu: kontrol yok, önce aramalar ve yeni ürünler
MAX_ELLE_BUTCE = 120


# ---------------------------------------------------------------- web sayfası (sade)

def _gecmis_ozet(con) -> dict:
    """Her ürün için stoktayken görülen en ucuz, en pahalı ve fiyat değişim sırası."""
    oz: dict = {}
    for r in con.execute("SELECT asin, fiyat FROM gecmis WHERE fiyat IS NOT NULL AND stokta=1 "
                         "ORDER BY asin, ts"):
        d = oz.setdefault(r["asin"], {"min": r["fiyat"], "max": r["fiyat"], "seri": []})
        d["min"] = min(d["min"], r["fiyat"])
        d["max"] = max(d["max"], r["fiyat"])
        if not d["seri"] or d["seri"][-1] != r["fiyat"]:  # art arda aynı fiyatları tek say
            d["seri"].append(r["fiyat"])
    return oz


def _degisim(r, d) -> str:
    if not r["stokta"] or not d or len(d["seri"]) < 2 or d["seri"][-1] != r["son_fiyat"]:
        return ""
    onceki, son = d["seri"][-2], d["seri"][-1]
    p = (son - onceki) * 100 / onceki
    yuzde = f"{abs(p):.1f}".replace(".", ",")
    if p < 0:
        return f'<span class="d">↓ %{yuzde}</span>'
    return f'<span class="z">↑ %{yuzde}</span>'


def site_uret(con, cfg) -> None:
    SITE.mkdir(exist_ok=True)
    e = html.escape
    oz = _gecmis_ozet(con)
    son = con.execute("SELECT * FROM calismalar ORDER BY rowid DESC LIMIT 1").fetchone()
    parca = [f"<h1>Amazon fiyat takibi</h1><p>Son güncelleme: {datetime.now():%d.%m.%Y %H:%M}<br>"
             "<small>Değişim: son fiyatın bir önceki farklı fiyata göre farkı. "
             "En ucuz / en pahalı: takip başladığından beri stoktayken görülen fiyatlar.</small></p>"]
    for t in cfg["takipler"]:
        if not t.get("aktif", True):
            continue
        rows = con.execute("SELECT * FROM urunler WHERE takip_id=? AND aktif=1 "
                           "ORDER BY stokta DESC, son_fiyat", (t["id"],)).fetchall()
        parca.append(f"<h2>{e(t['ad'])} <small>({len(rows)})</small></h2>")
        if not rows:
            parca.append("<p>Henüz ürün yok.</p>")
            continue
        parca.append('<div class="t"><table><thead><tr><th>Son fiyat</th><th>Değişim</th>'
                     '<th>En ucuz</th><th>En pahalı</th><th class="u">Ürün</th></tr></thead><tbody>')
        for r in rows:
            d = oz.get(r["asin"])
            url = f"{T.BASE}/dp/{r['asin']}" + ("?th=1&psc=1" if r["beden"] else "")
            fiyat = f"<b>{tl(r['son_fiyat'])}</b>" if r["stokta"] else "stokta yok"
            ek = f" [{e(r['beden'])}]" if r["beden"] else ""
            parca.append(f"<tr><td>{fiyat}</td><td>{_degisim(r, d)}</td>"
                         f"<td>{tl(d['min']) if d else '-'}</td><td>{tl(d['max']) if d else '-'}</td>"
                         f'<td class="u"><a href="{e(url)}">{e(r["baslik"] or r["asin"])}</a>{ek}</td></tr>')
        parca.append("</tbody></table></div>")
    if son:
        parca.append(f"<p><small>Son çalışma: {e(son['ts'][:16].replace('T', ' '))}, "
                     f"{son['sayfa']} sayfa, sonuç: {e(son['sonuc'])}</small></p>")
    css = (":root{color-scheme:light dark}body{font-family:-apple-system,sans-serif;max-width:1100px;"
           "margin:0 auto;padding:12px;line-height:1.45}.t{overflow-x:auto}"
           "table{border-collapse:collapse;width:100%;font-size:14px}"
           "th,td{padding:5px 8px;border-bottom:1px solid #8884;text-align:right;white-space:nowrap;vertical-align:top}"
           "th{font-weight:600}.u{text-align:left;white-space:normal;min-width:240px}"
           ".d{color:#2da44e;font-weight:600}.z{color:#e5534b;font-weight:600}")
    sayfa = ('<!doctype html><html lang="tr"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<meta name="robots" content="noindex"><title>Amazon fiyat takibi</title>'
             f'<style>{css}</style></head><body>{"".join(parca)}</body></html>')
    (SITE / "index.html").write_text(sayfa, encoding="utf-8")


# ---------------------------------------------------------------- ana akış

def veri_sil(con, tid: str) -> None:
    """config'deki veri_sil listesindeki takibin tüm kayıtlarını siler (tekrar çalışırsa iş yapmaz)."""
    n = con.execute("SELECT COUNT(*) FROM urunler WHERE takip_id=?", (tid,)).fetchone()[0]
    con.execute("DELETE FROM gecmis WHERE asin IN (SELECT asin FROM urunler WHERE takip_id=?)", (tid,))
    for tablo in ("urunler", "taranan", "adaylar"):
        con.execute(f"DELETE FROM {tablo} WHERE takip_id=?", (tid,))
    con.execute("DELETE FROM durum WHERE anahtar LIKE ? OR anahtar=?", (f"arama:{tid}:%", f"markasiz:{tid}"))
    con.commit()
    if n:
        log(f"'{tid}' takibinin {n} ürünü ve fiyat geçmişi silindi")


def main() -> None:
    cfg = T.ayarlari_yukle()
    a = cfg["ayarlar"]
    o = {**ONLINE_VARSAYILAN, **(cfg.get("online") or {})}
    # Elle başlatılan çalışmada GitHub'daki "Run workflow" kutusundan gelen değerler
    elle = os.environ.get("SAYFA_BUTCESI", "").strip()
    if elle.isdigit() and int(elle) > 0:
        o["sayfa_butcesi"] = min(int(elle), MAX_ELLE_BUTCE)
    mod = os.environ.get("MOD", "").strip() or "normal"
    sira = SIRA_KESIF if mod == "kesif" else SIRA
    log(f"Mod: {mod}, sayfa bütçesi: {o['sayfa_butcesi']}")
    con = T.db_baglan()
    con.executescript(EK_SCHEMA)
    for tid in cfg.get("veri_sil") or []:
        veri_sil(con, tid)
    for t in cfg["takipler"]:
        T.filtreyi_uygula(con, t)

    olaylar: list = []
    sonuc = "tamam"
    tr = None
    try:
        tr = OnlineTarayici(o)
        while tr.kalan() > 0:
            yapildi = False
            for tip in sira:
                if tr.kalan() <= 0:
                    break
                yapildi = GOREVLER[tip](tr, con, cfg, a, o, olaylar) or yapildi
            if not yapildi:
                log("Yapılacak iş kalmadı.")
                break
    except T.DogrulamaGerekli as ex:
        sonuc = "captcha"
        log(f"CAPTCHA yeniden denemede de çıktı ({ex}); bu çalışma burada bitti.")
    except T.LimitAsildi:
        sonuc = "limit"
    except Exception as ex:
        sonuc = "hata"
        log(f"HATA: {ex!r}")
    finally:
        if tr:
            tr.kapat()
        con.execute("INSERT INTO calismalar VALUES(?,?,?,?)",
                    (simdi_s(), tr.istek if tr else 0, tr.captcha if tr else 0, sonuc))
        con.commit()
        log(f"Bitti: {tr.istek if tr else 0} sayfa, {tr.captcha if tr else 0} CAPTCHA, sonuç: {sonuc}")
    site_uret(con, cfg)
    con.close()


if __name__ == "__main__":
    main()
