import asyncio; import sys; sys.path.insert(0,"."); from bot.utils.db import init_db; asyncio.run(init_db()); print("MIGRATION OK")
