# Nastavení limitu na 1 den v minutách (24 * 60 = 1440 minut)
LIMIT_MINUTES=1440

# Projít všechna worktree (vynechá se hlavní/root adresář)
git worktree list --porcelain | grep "^worktree " | awk '{print $2}' | while read -r wt_path; do

    # Cesta ke .git souboru uvnitř worktree
    GIT_FILE="$wt_path/.git"

    if [ -f "$GIT_FILE" ]; then
        # Zjistíme, zda byl soubor modifikován před více než 1440 minutami
        # (Funguje spolehlivě na Linuxu i macOS/Git Bash)
        if [ -n "$(find "$GIT_FILE" -mmin +"$LIMIT_MINUTES")" ]; then

            # Zjištění názvu věte v daném worktree
            BRANCH=$(git -C "$wt_path" branch --show-current)

            echo "Odebírám staré worktree (filesystem > 1 den): $wt_path"
            if [ -n "$BRANCH" ]; then
                echo "Příslušná větev ke smazání: $BRANCH"
            fi

            # 1. Odstranění worktree z disku i z Gitu
            git worktree remove "$wt_path" --force

            # 2. Smazání lokální věte
            if [ -n "$BRANCH" ]; then
                git branch -D "$BRANCH"
            fi
            echo "----------------------------------------"
        fi
    fi
done