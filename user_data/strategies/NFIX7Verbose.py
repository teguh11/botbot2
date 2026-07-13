"""
NFIX7Verbose — NostalgiaForInfinityX7 + penanda scanning/heartbeat yang jelas.

Murni additif: SEMUA logika trading didelegasikan ke NFI lewat super().
Tujuannya cuma satu — kasih log "aku lagi scanning" tiap menit supaya kamu
yakin bot hidup & bekerja, walau belum ada open trade (NFI sabar nunggu dip).

Log muncul di tab "Logs" FreqUI dan di `journalctl -u freqtrade -f`.
"""
import logging
from datetime import datetime

from freqtrade.persistence import Trade
from NostalgiaForInfinityX7 import NostalgiaForInfinityX7

logger = logging.getLogger(__name__)


class NFIX7Verbose(NostalgiaForInfinityX7):
    _last_scan_log: float = 0.0
    _scan_log_interval: int = 60  # detik antar log scanning

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        # jalankan seluruh logika NFI dulu
        super().bot_loop_start(current_time, **kwargs)

        # hanya di live / dry-run, dan di-throttle biar nggak spam
        if self.config["runmode"].value not in ("live", "dry_run"):
            return
        now = current_time.timestamp()
        if now - self._last_scan_log < self._scan_log_interval:
            return
        self._last_scan_log = now

        try:
            pairs = self.dp.current_whitelist()
            open_trades = Trade.get_open_trades()
            if open_trades:
                detail = ", ".join(
                    f"{t.pair}({'SHORT' if t.is_short else 'LONG'})"
                    for t in open_trades
                )
            else:
                detail = "belum ada — NFI masih nunggu setup dip"
            logger.info(
                f"🔍 SCAN aktif | {len(pairs)} pair dipantau | "
                f"{len(open_trades)} posisi terbuka: {detail}"
            )
        except Exception as e:
            logger.info(f"🔍 SCAN aktif | detail status tidak tersedia ({e})")
