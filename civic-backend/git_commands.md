# 🚀 Git Commands for Civic Backend

Useful commands for daily development on this project.

## 🌿 Branching

We use a **`development`** branch as our primary working branch.

- **Switch to development**: `git checkout development`
- **Create a new feature branch**: `git checkout -b feature/name-of-feature`
- **Merge feature into development**:
  ```bash
  git checkout development
  git merge feature/name-of-feature
  ```

## 🛠 Daily Workflow

1.  **Check status**: `git status`
2.  **Add changes**: `git add .` (or `git add path/to/file`)
3.  **Commit**: `git commit -m "feat: description of change"`
4.  **Push to development**: `git push origin development`
5.  **Pull latest changes**: `git pull origin development`

## ⏪ Mistakes & Undoing

- **Discard local changes in a file**: `git checkout -- path/to/file`
- **Unstage a file (keep changes)**: `git reset HEAD path/to/file`
- **Undo last commit (keep changes)**: `git reset --soft HEAD~1`
- **Undo last commit (destroy changes)**: `git reset --hard HEAD~1`

## 📦 Stashing (Temporary storage)

Useful if you need to switch branches but aren't ready to commit.

- **Save current local changes**: `git stash`
- **List stashes**: `git stash list`
- **Apply and remove latest stash**: `git stash pop`

## 📜 History & Logging

- **Compact history log**: `git log --oneline --graph --all`
- **Check commit diff**: `git show <commit-hash>`
- **Who changed what**: `git blame path/to/file`

## 📦 Handling Migrations (Alembic)

Always commit your migration files (`alembic/versions/`)!

1.  **Generate migration**: `alembic revision --autogenerate -m "description"`
2.  **Apply migration**: `alembic upgrade head`
3.  **Check migration history**: `alembic history --verbose`

## ⚠️ Important Tips

- **NEVER commit `.env`**: It's already in `.gitignore`, but double-check!
- **Commit often**: Small, atomic commits are easier to manage.
- **Pull before pushing**: Avoid merge conflicts by staying up-to-date.

---

### 📡 First-time Push (One-time setup)

If you haven't pushed your **`development`** branch yet:

1. Create a repository named **`civic-backend`** on GitHub.
2. Run:
   ```bash
   git push -u origin development
   ```
