"""
NFIX7Verbose — NostalgiaForInfinityX7 + log diagnostik kenapa belum entry.

Murni additif: SEMUA logika trading didelegasikan ke NFI lewat super().
Tiap ~60 dtk nge-log status scanning + ALASAN belum ada entry:
  - berapa pair yang PUNYA sinyal entry di candle terakhir
  - berapa slot posisi masih bebas
  - berapa pair sedang terkunci (cooldown/protection)
  - kesimpulan: memang belum ada sinyal, atau ada tapi keblok

Log muncul di tab "Logs" FreqUI, dashboard custom, dan `journalctl -u freqtrade`.
"""
import logging
from datetime import datetime

from freqtrade.persistence import Trade, PairLocks
from NostalgiaForInfinityX7 import NostalgiaForInfinityX7

logger = logging.getLogger(__name__)


class NFIX7Verbose(NostalgiaForInfinityX7):
    _last_scan_log: float = 0.0
    _scan_log_interval: int = 60  # detik antar log

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        # jalankan seluruh logika NFI dulu
        super().bot_loop_start(current_time, **kwargs)

        if self.config["runmode"].value not in ("live", "dry_run"):
            return
        now = current_time.timestamp()
        if now - self._last_scan_log < self._scan_log_interval:
            return
        self._last_scan_log = now

        try:
            pairs      = self.dp.current_whitelist()
            open_count = Trade.get_open_trade_count()
            max_open   = int(self.config.get("max_open_trades", 0) or 0)
            slots_free = max(max_open - open_count, 0)

            sig_long = sig_short = locked = no_data = 0
            signal_pairs = []

            for p in pairs:
                # pair sedang terkunci? (cooldown setelah exit / protection)
                if PairLocks.get_pair_locks(p, current_time):
                    locked += 1

                df, _ = self.dp.get_analyzed_dataframe(p, self.timeframe)
                if df is None or df.empty:
                    no_data += 1
                    continue

                last = df.iloc[-1]
                if last.get("enter_long") == 1:
                    sig_long += 1
                    signal_pairs.append(f"{p}·L")
                if last.get("enter_short") == 1:
                    sig_short += 1
                    signal_pairs.append(f"{p}·S")

            total_sig = sig_long + sig_short

            # ── tentukan ALASAN belum entry ──
            if open_count >= max_open and max_open > 0:
                reason = f"SLOT PENUH ({open_count}/{max_open}) — nunggu ada posisi tutup"
            elif total_sig == 0:
                reason = "belum ada sinyal — kriteria NFI belum terpenuhi (normal, nunggu dip)"
            else:
                # ada sinyal tapi belum kebuka → kemungkinan keblok
                blk = []
                if slots_free == 0:
                    blk.append("slot penuh")
                if locked:
                    blk.append(f"{locked} pair terkunci")
                extra = f" ({', '.join(blk)})" if blk else " — cek balance/lock/confirm_trade_entry"
                reason = f"ADA SINYAL [{', '.join(signal_pairs[:5])}] tapi belum entry{extra}"

            if no_data:
                reason += f" | {no_data} pair belum siap data (warmup)"

            logger.info(
                f"🔍 SCAN | {len(pairs)} pair | posisi {open_count}/{max_open} "
                f"(slot bebas {slots_free}) | sinyal skrg: {sig_long}L/{sig_short}S | "
                f"terkunci: {locked} → {reason}"
            )
        except Exception as e:
            logger.info(f"🔍 SCAN | diagnosa gagal: {e!r}")
