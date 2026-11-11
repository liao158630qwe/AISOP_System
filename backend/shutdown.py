"""Keep model cleanup alive until every shutdown step has been attempted."""
import asyncio
import logging


async def close_in_order(steps):
    """Drain in dependency order, even if the lifespan waiter is cancelled."""
    async def cleanup():
        first_error = None
        for name, close in steps:
            try:
                await close()
            except (Exception, asyncio.CancelledError) as exc:
                logging.getLogger(__name__).exception('Shutdown step failed: %s', name)
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    pending = asyncio.create_task(cleanup(), name='inference-shutdown')
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break  # The cleanup task logged the error and attempted every step.
    if cancelled:
        try:
            pending.result()
        except (Exception, asyncio.CancelledError):
            pass
        raise asyncio.CancelledError
    pending.result()
