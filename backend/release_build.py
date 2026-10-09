"""Run the normal dependency/build contract in a disposable, credential-free workspace."""
import asyncio
import os
import uuid

from agent import run_build


async def main():
    code, output = await run_build(uuid.UUID(os.environ['PROJECT_ID']))
    print(output, flush=True)
    raise SystemExit(code)


if __name__ == '__main__':
    asyncio.run(main())
