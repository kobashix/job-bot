"""Unified CLI for Job Bot 2.0 Redo."""
import asyncio
import sys
from main import run

async def main():
    print("="*40)
    print("   JOB BOT 2.0 - UNIFIED APPLY   ")
    print("="*40)
    
    code = await run()
    
    if code == 0:
        print("\n[SUCCESS] Operation completed.")
    else:
        print(f"\n[ERROR] Operation exited with code {code}.")
    
    sys.exit(code)

if __name__ == "__main__":
    asyncio.run(main())
