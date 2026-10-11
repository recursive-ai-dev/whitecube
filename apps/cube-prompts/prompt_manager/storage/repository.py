"""Repository abstraction for Prompt Manager persistence operations."""

from contextlib import closing
from datetime import datetime
import re
import sqlite3
from typing import List, Optional
import uuid

from prompt_manager.core.models import Folder, Prompt, PromptRevision, PromptTemplate, Tag
from prompt_manager.storage.database import Database


class PromptRepository:
    """Handles CRUD, querying, search, and revision history for prompts."""

    def __init__(self, db: Database):
        self.db = db

    def get_prompt_by_id(self, prompt_id: str) -> Optional[Prompt]:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute(
                """
                SELECT id, title, description, folder_id, template_id, template_content,
                       system_instruction, target_model, temperature, is_favorite,
                       use_count, created_at, updated_at
                FROM prompts WHERE id = ?
                """,
                (prompt_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            tags = self._get_tags_for_prompt(conn, prompt_id)
            return Prompt(
                id=row["id"],
                title=row["title"],
                description=row["description"] or "",
                folder_id=row["folder_id"],
                template_content=row["template_content"],
                system_instruction=row["system_instruction"] or "",
                target_model=row["target_model"],
                temperature=row["temperature"],
                is_favorite=bool(row["is_favorite"]),
                use_count=row["use_count"],
                tags=tags,
                template_id=row["template_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def list_prompts(
        self,
        folder_id: Optional[str] = None,
        tag_id: Optional[str] = None,
        favorite_only: bool = False,
        search_query: Optional[str] = None,
        template_id: Optional[str] = None,
    ) -> List[Prompt]:
        """Fetch prompts filtered by folder, tag, favorite, template, or FTS query."""
        with closing(self.db.get_connection()) as conn, conn:
            params = []
            conditions = []

            if favorite_only:
                conditions.append("p.is_favorite = 1")

            if folder_id:
                conditions.append("p.folder_id = ?")
                params.append(folder_id)

            if template_id:
                conditions.append("p.template_id = ?")
                params.append(template_id)

            if tag_id:
                conditions.append(
                    "p.id IN (SELECT prompt_id FROM prompt_tags WHERE tag_id = ?)"
                )
                params.append(tag_id)

            if search_query and search_query.strip():
                clean_query = self._sanitize_fts_query(search_query)
                if clean_query and self.db.fts_enabled:
                    conditions.append(
                        "p.id IN (SELECT id FROM prompts_fts WHERE prompts_fts MATCH ?)"
                    )
                    params.append(clean_query)
                else:
                    # Fallback to LIKE if query was purely punctuation, or when
                    # the SQLite build lacks the FTS5 module.
                    conditions.append(
                        "(p.title LIKE ? OR p.template_content LIKE ? OR p.description LIKE ?)"
                    )
                    like_term = f"%{search_query.strip()}%"
                    params.extend([like_term, like_term, like_term])

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            sql = f"""
                SELECT p.id, p.title, p.description, p.folder_id, p.template_id, p.template_content,
                       p.system_instruction, p.target_model, p.temperature, p.is_favorite,
                       p.use_count, p.created_at, p.updated_at
                FROM prompts p
                {where_clause}
                ORDER BY p.is_favorite DESC, p.updated_at DESC
            """

            cur = conn.execute(sql, params)
            prompts = []
            for row in cur.fetchall():
                prompt_id = row["id"]
                tags = self._get_tags_for_prompt(conn, prompt_id)
                prompts.append(
                    Prompt(
                        id=prompt_id,
                        title=row["title"],
                        description=row["description"] or "",
                        folder_id=row["folder_id"],
                        template_content=row["template_content"],
                        system_instruction=row["system_instruction"] or "",
                        target_model=row["target_model"],
                        temperature=row["temperature"],
                        is_favorite=bool(row["is_favorite"]),
                        use_count=row["use_count"],
                        tags=tags,
                        template_id=row["template_id"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            return prompts

    def save_prompt(self, prompt: Prompt, create_revision: bool = False) -> Prompt:
        """Create or update a prompt, updating tags and optionally creating a revision.

        Orphan policy (explicit, never crash): folder_id/template_id are
        nullified when the referenced row does not exist (matching the
        ON DELETE SET NULL delete behavior). Tags are canonicalized by the
        model (names); mapping names <-> ids is resolved here.
        """
        now = datetime.now().isoformat()
        with closing(self.db.get_connection()) as conn, conn:
            # Hold the write transaction across the snapshot and update.
            conn.execute("BEGIN IMMEDIATE")
            prompt.folder_id = self._nullify_missing_folder(conn, prompt.folder_id)
            prompt.template_id = self._nullify_missing_template(conn, prompt.template_id)
            # Check if prompt exists
            cur = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt.id,))
            previous = cur.fetchone()
            exists = previous is not None

            if exists:
                if create_revision:
                    self._record_revision(conn, Prompt(
                        id=previous["id"], title=previous["title"],
                        template_content=previous["template_content"],
                        system_instruction=previous["system_instruction"],
                    ))
                conn.execute(
                    """
                    UPDATE prompts SET
                        title = ?, description = ?, folder_id = ?, template_id = ?, template_content = ?,
                        system_instruction = ?, target_model = ?, temperature = ?,
                        is_favorite = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        prompt.title,
                        prompt.description,
                        prompt.folder_id,
                        prompt.template_id,
                        prompt.template_content,
                        prompt.system_instruction,
                        prompt.target_model,
                        prompt.temperature,
                        1 if prompt.is_favorite else 0,
                        now,
                        prompt.id,
                    ),
                )
                prompt.updated_at = now
            else:
                if not prompt.created_at:
                    prompt.created_at = now
                if not prompt.updated_at:
                    prompt.updated_at = now
                conn.execute(
                    """
                    INSERT INTO prompts (
                        id, title, description, folder_id, template_id, template_content,
                        system_instruction, target_model, temperature, is_favorite,
                        use_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prompt.id,
                        prompt.title,
                        prompt.description,
                        prompt.folder_id,
                        prompt.template_id,
                        prompt.template_content,
                        prompt.system_instruction,
                        prompt.target_model,
                        prompt.temperature,
                        1 if prompt.is_favorite else 0,
                        prompt.use_count,
                        prompt.created_at,
                        prompt.updated_at,
                    ),
                )

            # Update tags
            conn.execute("DELETE FROM prompt_tags WHERE prompt_id = ?", (prompt.id,))
            for tag_name in prompt.tags:
                clean_tag = tag_name.strip().lstrip("#").lower()
                if not clean_tag:
                    continue
                tag_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT OR IGNORE INTO tags (id, name) VALUES (?, ?)",
                    (tag_id, clean_tag),
                )
                cur = conn.execute("SELECT id FROM tags WHERE name = ?", (clean_tag,))
                tag_row = cur.fetchone()
                if tag_row:
                    conn.execute(
                        "INSERT OR IGNORE INTO prompt_tags (prompt_id, tag_id) VALUES (?, ?)",
                        (prompt.id, tag_row[0]),
                    )

        return prompt

    def delete_prompt(self, prompt_id: str) -> bool:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
            return cur.rowcount > 0

    def toggle_favorite(self, prompt_id: str) -> bool:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute(
                "SELECT is_favorite FROM prompts WHERE id = ?", (prompt_id,)
            )
            row = cur.fetchone()
            if not row:
                return False
            new_fav = 0 if row["is_favorite"] else 1
            conn.execute(
                "UPDATE prompts SET is_favorite = ? WHERE id = ?", (new_fav, prompt_id)
            )
            return bool(new_fav)

    def increment_use_count(self, prompt_id: str) -> None:
        with closing(self.db.get_connection()) as conn, conn:
            conn.execute(
                "UPDATE prompts SET use_count = use_count + 1 WHERE id = ?",
                (prompt_id,),
            )

    # ------------------ Folders ------------------

    def list_folders(self) -> List[Folder]:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute(
                "SELECT id, name, parent_id, icon, sort_order, created_at FROM folders ORDER BY sort_order, name ASC"
            )
            return [
                Folder(
                    id=row["id"],
                    name=row["name"],
                    parent_id=row["parent_id"],
                    icon=row["icon"],
                    sort_order=row["sort_order"],
                    created_at=row["created_at"],
                )
                for row in cur.fetchall()
            ]

    def save_folder(self, folder: Folder) -> Folder:
        """Create or update a folder; orphan parent_id nullified, never crash."""
        with closing(self.db.get_connection()) as conn, conn:
            folder.parent_id = self._nullify_missing_parent(conn, folder)
            conn.execute(
                """
                INSERT INTO folders (id, name, parent_id, icon, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    parent_id=excluded.parent_id,
                    icon=excluded.icon,
                    sort_order=excluded.sort_order
                """,
                (
                    folder.id,
                    folder.name,
                    folder.parent_id,
                    folder.icon,
                    folder.sort_order,
                    folder.created_at,
                ),
            )
        return folder

    def delete_folder(self, folder_id: str) -> bool:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            return cur.rowcount > 0

    # ------------------ Tags ------------------

    def list_tags(self) -> List[Tag]:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute("SELECT id, name, color FROM tags ORDER BY name ASC")
            return [
                Tag(id=row["id"], name=row["name"], color=row["color"])
                for row in cur.fetchall()
            ]

    def save_tag(self, tag: Tag) -> Tag:
        with closing(self.db.get_connection()) as conn, conn:
            conn.execute(
                """
                INSERT INTO tags (id, name, color)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, color=excluded.color
                """,
                (tag.id, tag.name, tag.color),
            )
        return tag

    def delete_tag(self, tag_id: str) -> bool:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            return cur.rowcount > 0

    # ------------------ Prompt Templates ------------------

    def get_template_by_id(self, template_id: str) -> Optional[PromptTemplate]:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute(
                """
                SELECT id, name, description, content, system_instruction,
                       category, created_at, updated_at
                FROM prompt_templates WHERE id = ?
                """,
                (template_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return PromptTemplate(
                id=row["id"],
                name=row["name"],
                description=row["description"] or "",
                content=row["content"] or "",
                system_instruction=row["system_instruction"] or "",
                category=row["category"] or "general",
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def get_template_by_name(self, name: str) -> Optional[PromptTemplate]:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute(
                """
                SELECT id, name, description, content, system_instruction,
                       category, created_at, updated_at
                FROM prompt_templates WHERE name = ?
                """,
                (name.strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            return PromptTemplate(
                id=row["id"],
                name=row["name"],
                description=row["description"] or "",
                content=row["content"] or "",
                system_instruction=row["system_instruction"] or "",
                category=row["category"] or "general",
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def list_templates(
        self,
        category: Optional[str] = None,
        search_query: Optional[str] = None,
    ) -> List[PromptTemplate]:
        """List templates, optionally filtered by category or FTS search."""
        with closing(self.db.get_connection()) as conn, conn:
            params: List[str] = []
            conditions: List[str] = []

            if category and category.strip() and category.strip().lower() != "all":
                conditions.append("t.category = ?")
                params.append(category.strip().lower())

            if search_query and search_query.strip():
                clean = self._sanitize_fts_query(search_query)
                if clean and self.db.fts_enabled:
                    conditions.append("t.id IN (SELECT id FROM templates_fts WHERE templates_fts MATCH ?)")
                    params.append(clean)
                else:
                    conditions.append(
                        "(t.name LIKE ? OR t.description LIKE ? OR t.content LIKE ?)"
                    )
                    like = f"%{search_query.strip()}%"
                    params.extend([like, like, like])

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            sql = f"""
                SELECT t.id, t.name, t.description, t.content, t.system_instruction,
                       t.category, t.created_at, t.updated_at
                FROM prompt_templates t
                {where}
                ORDER BY t.updated_at DESC, t.name ASC
            """
            cur = conn.execute(sql, params)
            return [
                PromptTemplate(
                    id=row["id"],
                    name=row["name"],
                    description=row["description"] or "",
                    content=row["content"] or "",
                    system_instruction=row["system_instruction"] or "",
                    category=row["category"] or "general",
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in cur.fetchall()
            ]

    def save_template(self, template: PromptTemplate) -> PromptTemplate:
        """Create or update a template. Enforces unique name via manual check."""
        now = datetime.now().isoformat()
        # Normalize
        template.name = template.name.strip()
        if not template.name:
            raise ValueError("Template name must not be empty")
        if not template.content or not template.content.strip():
            raise ValueError("Template content must not be empty")
        template.category = (template.category or "general").strip().lower() or "general"

        with closing(self.db.get_connection()) as conn, conn:
            # Check unique name conflict
            cur = conn.execute(
                "SELECT id FROM prompt_templates WHERE name = ? AND id != ?",
                (template.name, template.id),
            )
            if cur.fetchone():
                raise ValueError(f"A template named '{template.name}' already exists")

            cur = conn.execute("SELECT id FROM prompt_templates WHERE id = ?", (template.id,))
            exists = cur.fetchone() is not None

            if exists:
                conn.execute(
                    """
                    UPDATE prompt_templates SET
                        name = ?, description = ?, content = ?, system_instruction = ?,
                        category = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        template.name,
                        template.description,
                        template.content,
                        template.system_instruction,
                        template.category,
                        now,
                        template.id,
                    ),
                )
                template.updated_at = now
            else:
                if not template.created_at:
                    template.created_at = now
                if not template.updated_at:
                    template.updated_at = now
                conn.execute(
                    """
                    INSERT INTO prompt_templates (id, name, description, content, system_instruction, category, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        template.id,
                        template.name,
                        template.description,
                        template.content,
                        template.system_instruction,
                        template.category,
                        template.created_at,
                        template.updated_at,
                    ),
                )
        return template

    def delete_template(self, template_id: str) -> bool:
        """Delete a template. Prompts referencing it will have template_id set to NULL via FK."""
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute("DELETE FROM prompt_templates WHERE id = ?", (template_id,))
            return cur.rowcount > 0

    def create_prompt_from_template(
        self,
        template_id: str,
        title: Optional[str] = None,
        folder_id: Optional[str] = None,
        extra_tags: Optional[List[str]] = None,
    ) -> Prompt:
        """Instantiate a new Prompt from a template's content/system_instruction."""
        tmpl = self.get_template_by_id(template_id)
        if not tmpl:
            raise ValueError(f"Template not found: {template_id}")
        prompt = Prompt(
            id=str(uuid.uuid4()),
            title=title or tmpl.name,
            description=tmpl.description,
            folder_id=folder_id,
            template_content=tmpl.content,
            system_instruction=tmpl.system_instruction,
            template_id=tmpl.id,
            tags=list(extra_tags or []),
        )
        return self.save_prompt(prompt)

    # ------------------ Revisions ------------------

    def get_revisions(self, prompt_id: str) -> List[PromptRevision]:
        with closing(self.db.get_connection()) as conn, conn:
            cur = conn.execute(
                """
                SELECT id, prompt_id, revision_number, title, template_content,
                       system_instruction, created_at
                FROM prompt_revisions
                WHERE prompt_id = ?
                ORDER BY revision_number DESC
                """,
                (prompt_id,),
            )
            return [
                PromptRevision(
                    id=row["id"],
                    prompt_id=row["prompt_id"],
                    revision_number=row["revision_number"],
                    title=row["title"],
                    template_content=row["template_content"],
                    system_instruction=row["system_instruction"] or "",
                    created_at=row["created_at"],
                )
                for row in cur.fetchall()
            ]

    def _record_revision(self, conn: sqlite3.Connection, prompt: Prompt) -> None:
        cur = conn.execute(
            "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM prompt_revisions WHERE prompt_id = ?",
            (prompt.id,),
        )
        next_rev = cur.fetchone()[0]
        conn.execute(
            """
            INSERT INTO prompt_revisions (
                id, prompt_id, revision_number, title, template_content,
                system_instruction, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                str(uuid.uuid4()),
                prompt.id,
                next_rev,
                prompt.title,
                prompt.template_content,
                prompt.system_instruction,
            ),
        )

    # ------------------ Helpers ------------------

    def _exists(self, conn: sqlite3.Connection, table: str, row_id: Optional[str]) -> bool:
        if not row_id:
            return False
        cur = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,))
        return cur.fetchone() is not None

    def _nullify_missing_folder(self, conn: sqlite3.Connection, folder_id: Optional[str]) -> Optional[str]:
        if folder_id and not self._exists(conn, "folders", folder_id):
            return None
        return folder_id

    def _nullify_missing_template(self, conn: sqlite3.Connection, template_id: Optional[str]) -> Optional[str]:
        if template_id and not self._exists(conn, "prompt_templates", template_id):
            return None
        return template_id

    def _nullify_missing_parent(self, conn: sqlite3.Connection, folder: Folder) -> Optional[str]:
        parent_id = folder.parent_id
        if not parent_id:
            return None
        if parent_id == folder.id:
            return None
        if not self._exists(conn, "folders", parent_id):
            return None
        return parent_id

    def _get_tags_for_prompt(self, conn: sqlite3.Connection, prompt_id: str) -> List[str]:
        cur = conn.execute(
            """
            SELECT t.name FROM tags t
            JOIN prompt_tags pt ON pt.tag_id = t.id
            WHERE pt.prompt_id = ?
            ORDER BY t.name ASC
            """,
            (prompt_id,),
        )
        return [r[0] for r in cur.fetchall()]

    def _sanitize_fts_query(self, query: str) -> str:
        """Escape and format a search query for SQLite FTS5 prefix token matching."""
        tokens = re.findall(r"\w+", query)
        if not tokens:
            return ""
        # Match tokens as prefixes for responsive instant search: e.g. "code ref*"
        formatted = " ".join([f'"{token}"*' for token in tokens])
        return formatted
