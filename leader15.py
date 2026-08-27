"""
SST 15분봉 스캔 실행 스크립트
사용법: python3 leader15.py
"""
import os
os.environ["SST_TIMEFRAME"] = "15m"

from leader import SSTLeader

if __name__ == "__main__":
    SSTLeader().run_daily_scan()
