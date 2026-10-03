import calendar
import hashlib
import html
from datetime import date, timedelta

import altair as alt
import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Winter Arc Tracker", page_icon="❄️", layout="wide")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
HEADERS = {
    "users": ["name", "pin_hash"],
    "habits": ["user", "habit"],
    "checkins": ["date", "user", "habit", "done"],
}
PALETTE = ["#00E5FF", "#FF4DA6", "#FFD54F", "#69F0AE"]

CSS = """
<style>
h1 {
    background: linear-gradient(90deg, #00E5FF, #B388FF);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 800;
}
[data-testid="stMetric"] {
    background: #0E1A2B;
    border: 1px solid #1B3350;
    border-radius: 16px;
    padding: 14px 18px;
    box-shadow: 0 0 18px rgba(0, 229, 255, 0.08);
}
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0 14px 0; }
.chip {
    background: #0E1A2B;
    border: 1px solid #1B3350;
    border-radius: 999px;
    padding: 6px 14px;
    font-size: 0.9rem;
}
.chip small { opacity: 0.7; }
</style>
"""


# ---------- Google Sheets helpers ----------
@st.cache_resource
def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES
    )
    sh = gspread.authorize(creds).open_by_key(st.secrets["sheet_id"])
    existing = {ws.title for ws in sh.worksheets()}
    for title, headers in HEADERS.items():
        if title not in existing:
            ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.append_row(headers, value_input_option="RAW")
    return sh


@st.cache_data(ttl=15, show_spinner=False)
def read(title):
    """All rows of a worksheet as a list of dicts (values are strings).
    Cached for a few seconds so the app stays inside Google's rate limits."""
    values = get_spreadsheet().worksheet(title).get_all_values()
    if len(values) < 2:
        return []
    head = values[0]
    return [dict(zip(head, row)) for row in values[1:]]


def clear_cache():
    st.cache_data.clear()


def append(title, row):
    get_spreadsheet().worksheet(title).append_row(row, value_input_option="RAW")
    clear_cache()


# ---------- Users ----------
def hash_pin(name, pin):
    return hashlib.sha256(f"{name.lower()}:{pin}".encode()).hexdigest()


def sign_up(name, pin):
    name = name.strip()
    if not name or not pin.isdigit() or len(pin) < 4:
        return "Enter a name and a PIN of at least 4 digits."
    if any(u["name"].lower() == name.lower() for u in read("users")):
        return "That name is already taken."
    append("users", [name, hash_pin(name, pin)])
    st.session_state.user = name
    return None


def log_in(name, pin):
    for u in read("users"):
        if u["name"] == name and u["pin_hash"] == hash_pin(name, pin):
            st.session_state.user = name
            return True
    return False


# ---------- Habits ----------
def get_habits(user):
    return [h["habit"] for h in read("habits") if h["user"] == user]


def add_habit(user, habit):
    habit = habit.strip()
    if not habit:
        return "Type a habit first."
    if habit.lower() in [h.lower() for h in get_habits(user)]:
        return "You already have that habit."
    append("habits", [user, habit])
    return None


def remove_habit(user, habit):
    ws = get_spreadsheet().worksheet("habits")
    values = ws.get_all_values()
    # delete from the bottom so row numbers stay valid
    for i in range(len(values), 1, -1):
        if values[i - 1][0] == user and values[i - 1][1] == habit:
            ws.delete_rows(i)
    clear_cache()


def delete_account(user, pin):
    """Delete the login, habits and check-ins of a user. Returns False if the PIN is wrong."""
    sh = get_spreadsheet()
    users = sh.worksheet("users").get_all_values()  # fresh read, not cached
    expected = hash_pin(user, pin)
    if not any(r[0] == user and len(r) > 1 and r[1] == expected for r in users[1:]):
        return False

    # (sheet, column that holds the user's name); the users sheet goes last
    for title, col in (("checkins", 1), ("habits", 0), ("users", 0)):
        ws = sh.worksheet(title)
        values = ws.get_all_values()
        keep = [values[0]] + [r for r in values[1:] if not (len(r) > col and r[col] == user)]
        if len(keep) != len(values):
            ws.clear()
            ws.update(values=keep, range_name="A1", value_input_option="RAW")
    clear_cache()
    return True


# ---------- Check-ins ----------
def save_many(user, items):
    """items = [(iso_date, habit, done_bool), ...]; updates existing rows, appends new ones."""
    ws = get_spreadsheet().worksheet("checkins")
    values = ws.get_all_values()  # fresh read, not cached
    index = {
        (r[0], r[1], r[2]): i + 1
        for i, r in enumerate(values)
        if i > 0 and len(r) >= 3
    }
    updates, new_rows = [], []
    for day, habit, done in items:
        flag = "1" if done else "0"
        key = (day, user, habit)
        if key in index:
            updates.append({"range": f"D{index[key]}", "values": [[flag]]})
        else:
            new_rows.append([day, user, habit, flag])
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
    clear_cache()


def checkins_df():
    df = pd.DataFrame(read("checkins"), columns=HEADERS["checkins"])
    df["done"] = df["done"] == "1"
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date"])


def compute_streaks(user, habits):
    """Return {habit: (current_streak, best_streak)} from saved check-ins."""
    done = {}
    for c in read("checkins"):
        if c["user"] == user and c["done"] == "1":
            try:
                done.setdefault(c["habit"], set()).add(date.fromisoformat(c["date"]))
            except ValueError:
                pass

    today = date.today()
    result = {}
    for habit in habits:
        days = done.get(habit, set())

        best = run = 0
        prev = None
        for d in sorted(days):
            run = run + 1 if prev and (d - prev).days == 1 else 1
            best = max(best, run)
            prev = d

        # a streak stays alive until a full day is missed:
        # count back from today if it is ticked, otherwise from yesterday
        current = 0
        d = today if today in days else today - timedelta(days=1)
        while d in days:
            current += 1
            d -= timedelta(days=1)

        result[habit] = (current, best)
    return result


def daily_completion(df, user, habits, start, end):
    """Percent of this user's habits done on each day from start to end."""
    days = pd.date_range(start, end)
    if not habits:
        return pd.Series(0.0, index=days)
    sub = df[(df["user"] == user) & df["habit"].isin(habits) & df["done"]]
    counts = sub.groupby("date")["habit"].nunique().reindex(days, fill_value=0)
    return counts / len(habits) * 100


def habit_completion(df, user, habits, start, end):
    """Percent of days from start to end on which each habit was done."""
    n_days = (end - start).days + 1
    sub = df[
        (df["user"] == user)
        & df["done"]
        & (df["date"] >= pd.Timestamp(start))
        & (df["date"] <= pd.Timestamp(end))
    ]
    counts = sub.groupby("habit")["date"].nunique()
    return pd.DataFrame(
        {"habit": habits, "pct": [counts.get(h, 0) / n_days * 100 for h in habits]}
    )


# ---------- UI ----------
def login_screen():
    st.title("❄️ Winter Arc Tracker")
    st.caption("Build the habits. Keep the streak. Beat your friend (nicely).")
    if st.session_state.pop("account_deleted", False):
        st.success("Your account was deleted.")
    tab_in, tab_up = st.tabs(["Log in", "Sign up"])

    with tab_in:
        names = [u["name"] for u in read("users")]
        if not names:
            st.info("No one has signed up yet. Use the Sign up tab.")
        else:
            with st.form("login"):
                name = st.selectbox("Name", names)
                pin = st.text_input("PIN", type="password")
                if st.form_submit_button("Log in"):
                    if log_in(name, pin):
                        st.rerun()
                    else:
                        st.error("Wrong PIN.")

    with tab_up:
        with st.form("signup"):
            name = st.text_input("Your name")
            pin = st.text_input("Choose a PIN (4+ digits)", type="password")
            if st.form_submit_button("Create account"):
                error = sign_up(name, pin)
                if error:
                    st.error(error)
                else:
                    st.rerun()


def month_tab(user):
    if st.session_state.pop("just_saved", False):
        st.toast("Saved ✅")

    users = [u["name"] for u in read("users")]
    today = date.today()

    c_who, c_year, c_month = st.columns([2, 1, 2])
    who = c_who.selectbox("Whose grid?", users, index=users.index(user))
    year = int(c_year.number_input("Year", min_value=2024, max_value=2100, value=today.year, step=1))
    month = c_month.selectbox(
        "Month",
        list(range(1, 13)),
        index=today.month - 1,
        format_func=lambda m: calendar.month_name[m],
    )
    editable = who == user

    habits = get_habits(who)
    if not habits:
        st.info(f"{who} has no habits yet. Add some in the My habits tab.")
        return

    # current streaks as little chips
    streaks = compute_streaks(who, habits)
    chips = "".join(
        f'<span class="chip">{"🔥" if cur else "▫️"} {html.escape(h)} · {cur} '
        f"<small>(best {best})</small></span>"
        for h, (cur, best) in streaks.items()
    )
    st.markdown(f'<div class="chips">{chips}</div>', unsafe_allow_html=True)

    # build the grid from saved data
    prefix = f"{year}-{month:02d}-"
    saved = {
        (c["date"], c["habit"]): c["done"] == "1"
        for c in read("checkins")
        if c["user"] == who and c["date"].startswith(prefix)
    }
    n_days = calendar.monthrange(year, month)[1]
    rows = []
    for h in habits:
        row = {"Habit": h}
        for d in range(1, n_days + 1):
            row[str(d)] = saved.get((date(year, month, d).isoformat(), h), False)
        rows.append(row)
    base = pd.DataFrame(rows)

    config = {"Habit": st.column_config.TextColumn("Habit", width="medium")}
    for d in range(1, n_days + 1):
        config[str(d)] = st.column_config.CheckboxColumn(str(d), width="small")

    future_cols = [
        str(d) for d in range(1, n_days + 1) if date(year, month, d) > today
    ]
    disabled = True if not editable else ["Habit"] + future_cols

    edited = st.data_editor(
        base,
        column_config=config,
        hide_index=True,
        disabled=disabled,
        key=f"grid_{who}_{year}_{month}_{st.session_state.grid_v}",
    )

    if not editable:
        st.caption(f"You are viewing {who}'s grid (read-only).")
        return

    changes = []
    for i, h in enumerate(habits):
        for d in range(1, n_days + 1):
            old = bool(base.at[i, str(d)])
            new = bool(edited.at[i, str(d)])
            if old != new:
                changes.append((date(year, month, d).isoformat(), h, new))

    if st.button(f"Save changes ({len(changes)})", type="primary", disabled=not changes):
        save_many(user, changes)
        st.session_state.grid_v += 1
        st.session_state.just_saved = True
        st.rerun()
    st.caption("Tick the boxes, then press Save. Future days are locked.")


def progress_tab(user):
    users = [u["name"] for u in read("users")]
    df = checkins_df()
    today = date.today()
    first = today.replace(day=1)
    habits = get_habits(user)

    st.subheader(f"{user}: {calendar.month_name[today.month]} so far")
    series = daily_completion(df, user, habits, first, today)
    streaks = compute_streaks(user, habits)
    m1, m2, m3 = st.columns(3)
    m1.metric("Completion", f"{series.mean():.0f}%")
    m2.metric("Perfect days", int((series >= 99.99).sum()))
    m3.metric("Longest live streak", max([c for c, _ in streaks.values()], default=0))

    st.subheader("Daily completion: you vs friend")
    frames = []
    for u in users:
        s = daily_completion(df, u, get_habits(u), first, today)
        frames.append(pd.DataFrame({"date": s.index, "user": u, "pct": s.values}))
    data = pd.concat(frames, ignore_index=True)
    area = (
        alt.Chart(data)
        .mark_area(line={"strokeWidth": 2}, opacity=0.25, interpolate="monotone")
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%d %b")),
            y=alt.Y("pct:Q", title="% of habits done", scale=alt.Scale(domain=[0, 100])),
            color=alt.Color(
                "user:N",
                scale=alt.Scale(range=PALETTE),
                legend=alt.Legend(title=None, orient="top"),
            ),
            tooltip=[
                alt.Tooltip("user:N", title="Who"),
                alt.Tooltip("date:T", title="Day", format="%d %b"),
                alt.Tooltip("pct:Q", title="Done %", format=".0f"),
            ],
        )
        .properties(width="container", height=280)
    )
    st.altair_chart(area)

    if habits:
        st.subheader("Which habits are strongest?")
        hc = habit_completion(df, user, habits, first, today)
        bars = (
            alt.Chart(hc)
            .mark_bar(cornerRadiusEnd=6, color=PALETTE[0])
            .encode(
                x=alt.X("pct:Q", title="% of days done", scale=alt.Scale(domain=[0, 100])),
                y=alt.Y("habit:N", sort="-x", title=None),
                tooltip=[
                    alt.Tooltip("habit:N", title="Habit"),
                    alt.Tooltip("pct:Q", title="Days done %", format=".0f"),
                ],
            )
            .properties(width="container", height=max(120, 44 * len(hc)))
        )
        st.altair_chart(bars)

    st.subheader("🏆 This week's leaderboard")
    week_start = today - timedelta(days=today.weekday())  # Monday
    board = []
    for u in users:
        s = daily_completion(df, u, get_habits(u), week_start, today)
        board.append((u, float(s.mean())))
    board.sort(key=lambda x: -x[1])
    medals = ["🥇", "🥈", "🥉"]
    for rank, (u, pct) in enumerate(board):
        medal = medals[rank] if rank < len(medals) else "▫️"
        st.write(f"{medal} **{u}**: {pct:.0f}%")
        st.progress(min(pct / 100, 1.0))


def habits_tab(user):
    with st.form("add_habit", clear_on_submit=True):
        new = st.text_input("New habit (e.g. Workout 30 min)")
        if st.form_submit_button("Add habit"):
            error = add_habit(user, new)
            if error:
                st.error(error)
            else:
                st.rerun()

    habits = get_habits(user)
    if habits:
        st.subheader("Your habits")
        for h in habits:
            st.write(f"• {h}")
        to_remove = st.selectbox("Remove a habit", ["—"] + habits)
        if to_remove != "—" and st.button("Remove"):
            remove_habit(user, to_remove)
            st.rerun()

    st.divider()
    with st.expander("⚠️ Delete my account"):
        st.warning(
            "This permanently deletes your login, your habits and your whole "
            "check-in history. It cannot be undone."
        )
        pin = st.text_input("Enter your PIN to confirm", type="password", key="del_pin")
        sure = st.checkbox("I understand this cannot be undone", key="del_sure")
        if st.button("Delete my account permanently", disabled=not sure):
            if delete_account(user, pin):
                for key in ("user", "del_pin", "del_sure"):
                    st.session_state.pop(key, None)
                st.session_state.account_deleted = True
                st.rerun()
            else:
                st.error("Wrong PIN.")


def main():
    st.markdown(CSS, unsafe_allow_html=True)
    st.session_state.setdefault("grid_v", 0)

    if "user" not in st.session_state:
        login_screen()
        return

    user = st.session_state.user
    st.sidebar.write(f"Logged in as **{user}**")
    if st.sidebar.button("Log out"):
        del st.session_state["user"]
        st.rerun()

    st.title("❄️ Winter Arc")
    tab_month, tab_progress, tab_habits = st.tabs(["📅 Month", "📊 Progress", "⚙️ My habits"])
    with tab_month:
        month_tab(user)
    with tab_progress:
        progress_tab(user)
    with tab_habits:
        habits_tab(user)


main()