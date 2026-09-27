"""Estado de la cola: la maquina de estados de SPEC.md, seccion 5.

    queued ──→ downloading ──→ ready ──→ playing ──→ played
                  │
                  └──→ failed

Ademas `removed` desde cualquier estado activo. Todo es sincrono y vive en
SQLite; la orquestacion (hilos de descarga, broadcast por WebSocket) es de
main.py, que llama estos metodos y luego transmite `snapshot()`.
"""

import sqlite3
import threading

from app import db

MAX_CONCURRENT_DOWNLOADS = 2

# Estados que cuentan como "en la cola" (pendientes de sonar).
PENDING = ("queued", "downloading", "ready")


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class Queue:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # Las descargas terminan en otros hilos; serializamos todo acceso.
        self.lock = threading.RLock()

    # --- lectura ------------------------------------------------------------

    def _items(self, where: str, params: tuple = ()) -> list[dict]:
        rows = self.conn.execute(
            f"""
            SELECT q.id, q.video_id, q.session_id, q.requested_by, q.state,
                   q.position, q.error, q.added_at, q.played_at,
                   s.title, s.channel, s.duration_s, s.thumb_url
            FROM queue q JOIN songs s ON s.video_id = q.video_id
            WHERE {where}
            ORDER BY q.position, q.id
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, item_id: int) -> dict | None:
        items = self._items("q.id = ?", (item_id,))
        return items[0] if items else None

    def current(self) -> dict | None:
        items = self._items("q.state = 'playing'")
        return items[0] if items else None

    def pending(self) -> list[dict]:
        return self._items("q.state IN ('queued','downloading','ready')")

    def failed(self) -> list[dict]:
        return self._items("q.state = 'failed'")

    def next_ready(self) -> dict | None:
        items = self._items("q.state = 'ready'")
        return items[0] if items else None

    def snapshot(self) -> dict:
        """Estado completo que se manda a los clientes en el mensaje `state`."""
        with self.lock:
            current = self.current()
            pending = self.pending()
            return {
                "mode": "reproduccion" if current else "espera",
                "current": current,
                "queue": pending,
                # Lo que la barra de la pantalla muestra como "siguientes".
                "up_next": pending[:2],
                # Quien sonara realmente despues (regla de avance).
                "next_ready": self.next_ready(),
                "failed": self.failed(),
            }

    # --- alta ---------------------------------------------------------------

    def _next_position(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM queue").fetchone()
        return row[0]

    def add(self, video_id: str, session: dict) -> dict:
        """Agrega una peticion. Si la cancion ya esta en cache entra directo en `ready`.

        La cancion debe existir en `songs` (main.py la registra desde el resultado
        de busqueda con db.upsert_song antes de llamar esto).
        """
        with self.lock:
            if db.get_song(self.conn, video_id) is None:
                raise NotFound(f"cancion desconocida: {video_id}")
            state = "ready" if db.cached_file(self.conn, video_id) else "queued"
            cur = self.conn.execute(
                """
                INSERT INTO queue (video_id, session_id, requested_by, state, position)
                VALUES (?, ?, ?, ?, ?)
                """,
                (video_id, session["id"], session["name"], state, self._next_position()),
            )
            return self.get(cur.lastrowid)

    # --- descargas ----------------------------------------------------------

    def claim_downloads(self) -> list[str]:
        """Pasa a `downloading` las siguientes peticiones en cola, respetando el limite.

        Devuelve los video_id que main.py debe empezar a descargar. Si el mismo
        video ya se esta bajando para otra peticion no se baja dos veces: esa
        peticion espera en `queued` y se marca lista junto con la otra.
        """
        with self.lock, db.transaction(self.conn):
            active = {
                r[0]
                for r in self.conn.execute(
                    "SELECT DISTINCT video_id FROM queue WHERE state = 'downloading'"
                )
            }
            slots = MAX_CONCURRENT_DOWNLOADS - len(active)
            started: list[str] = []
            for item in self._items("q.state = 'queued'"):
                if slots <= 0:
                    break
                vid = item["video_id"]
                if vid in active:
                    continue
                if db.cached_file(self.conn, vid):
                    # Bajo mientras esperaba (otra peticion del mismo video).
                    self.conn.execute(
                        "UPDATE queue SET state = 'ready' WHERE id = ?", (item["id"],)
                    )
                    continue
                self.conn.execute(
                    "UPDATE queue SET state = 'downloading' WHERE video_id = ? AND state = 'queued'",
                    (vid,),
                )
                active.add(vid)
                started.append(vid)
                slots -= 1
            return started

    def mark_downloaded(self, video_id: str, file_path: str) -> None:
        with self.lock, db.transaction(self.conn):
            db.set_song_file(self.conn, video_id, file_path)
            self.conn.execute(
                "UPDATE queue SET state = 'ready', error = NULL "
                "WHERE video_id = ? AND state IN ('queued','downloading')",
                (video_id,),
            )

    def mark_download_failed(self, video_id: str, error: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE queue SET state = 'failed', error = ? "
                "WHERE video_id = ? AND state IN ('queued','downloading')",
                (error, video_id),
            )

    # --- reproduccion -------------------------------------------------------

    def _start_next(self) -> dict | None:
        """Regla de avance: la primera en `ready` por posicion. Las que siguen
        bajando conservan su lugar. Sin nada listo, la pantalla queda en espera."""
        nxt = self.next_ready()
        if nxt is None:
            return None
        self.conn.execute(
            "UPDATE queue SET state = 'playing', played_at = datetime('now') WHERE id = ?",
            (nxt["id"],),
        )
        self.conn.execute(
            "UPDATE songs SET play_count = play_count + 1 WHERE video_id = ?",
            (nxt["video_id"],),
        )
        return self.get(nxt["id"])

    def start_if_idle(self) -> dict | None:
        """Si no suena nada y hay algo listo, lo arranca. Devuelve lo que empezo."""
        with self.lock, db.transaction(self.conn):
            if self.current() is not None:
                return None
            return self._start_next()

    def advance(self, expected_id: int | None = None) -> dict | None:
        """Termina la actual (`played`) y arranca la siguiente lista.

        `expected_id` protege contra un `ended` duplicado o tardio: si la que
        suena ya no es esa, no se hace nada.
        """
        with self.lock, db.transaction(self.conn):
            cur = self.current()
            if cur is not None:
                if expected_id is not None and cur["id"] != expected_id:
                    return None
                self.conn.execute("UPDATE queue SET state = 'played' WHERE id = ?", (cur["id"],))
            elif expected_id is not None:
                return None
            return self._start_next()

    def fail_current(self, error: str, expected_id: int | None = None) -> dict | None:
        """La pantalla no pudo reproducir: `failed` y avanzar."""
        with self.lock, db.transaction(self.conn):
            cur = self.current()
            if cur is None or (expected_id is not None and cur["id"] != expected_id):
                return None
            self.conn.execute(
                "UPDATE queue SET state = 'failed', error = ? WHERE id = ?", (error, cur["id"])
            )
            return self._start_next()

    # --- edicion ------------------------------------------------------------

    def remove(self, item_id: int, session: dict) -> bool:
        """Quita una peticion (propia, o cualquiera si es admin).

        Devuelve True si se quito la que sonaba (y entonces ya arranco la siguiente).
        """
        with self.lock, db.transaction(self.conn):
            item = self.get(item_id)
            if item is None or item["state"] in ("played", "removed"):
                raise NotFound(f"no existe la peticion {item_id}")
            if not session.get("is_admin") and item["session_id"] != session["id"]:
                raise Forbidden("solo puedes quitar tus propias canciones")
            self.conn.execute("UPDATE queue SET state = 'removed' WHERE id = ?", (item_id,))
            if item["state"] == "playing":
                self._start_next()
                return True
            return False

    def reorder(self, ordered_ids: list[int]) -> None:
        """Reasigna posiciones a las pendientes en el orden dado (solo admin).

        Las pendientes que no vengan en la lista se quedan al final en su orden
        actual, para que una lista desactualizada no pierda canciones.
        """
        with self.lock, db.transaction(self.conn):
            pending = [i["id"] for i in self.pending()]
            pending_set = set(pending)
            order = [i for i in dict.fromkeys(ordered_ids) if i in pending_set]
            given = set(order)
            order += [i for i in pending if i not in given]
            base = self.conn.execute(
                "SELECT COALESCE(MIN(position), 1) FROM queue WHERE state IN ('queued','downloading','ready')"
            ).fetchone()[0]
            for offset, item_id in enumerate(order):
                self.conn.execute(
                    "UPDATE queue SET position = ? WHERE id = ?", (base + offset, item_id)
                )

    def move(self, item_id: int, delta: int) -> None:
        """Sube (delta < 0) o baja (delta > 0) una peticion pendiente."""
        with self.lock:
            ids = [i["id"] for i in self.pending()]
            if item_id not in ids:
                raise NotFound(f"no esta en la cola: {item_id}")
            idx = ids.index(item_id)
            new = max(0, min(len(ids) - 1, idx + delta))
            ids.insert(new, ids.pop(idx))
            self.reorder(ids)

    def clear(self) -> None:
        """Vacia la cola pendiente y los fallidos. La que suena sigue sonando."""
        with self.lock:
            self.conn.execute(
                "UPDATE queue SET state = 'removed' "
                "WHERE state IN ('queued','downloading','ready','failed')"
            )

    def recover(self) -> None:
        """Al arrancar: las descargas interrumpidas vuelven a la cola."""
        with self.lock:
            self.conn.execute("UPDATE queue SET state = 'queued' WHERE state = 'downloading'")
