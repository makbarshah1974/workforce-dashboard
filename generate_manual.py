# -*- coding: utf-8 -*-
# Generate a User Manual PDF (Workforce & Machine Live Monitoring System).
# Standard library only.
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "User_Manual.pdf")


def esc(s):
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Doc:
    def __init__(self):
        self.W = 595.28
        self.H = 841.89
        self.margin = 56
        self.pages = []
        self.cur = []
        self.y = self.H - self.margin

    def _newpage(self):
        self.pages.append(self.cur)
        self.cur = []
        self.y = self.H - self.margin

    def _ensure(self, h):
        if self.y - h < self.margin:
            self._newpage()

    def hline(self, y, x1, x2, gray=0.6, w=0.7):
        self.cur.append(("LINE", y, x1, x2, w, gray))

    def write_block(self, lines, x, font, size, leading,
                    gap_before=0, gap_after=0, center=False):
        if gap_before:
            self._ensure(gap_before)
        factor = 0.56 if font == "F2" else 0.52
        for line in lines:
            if self.y - leading < self.margin:
                self._newpage()
            ypos = self.y
            if center and line:
                tw = len(line) * size * factor
                xpos = max(self.margin, (self.W - tw) / 2)
            else:
                xpos = x
            self.cur.append((xpos, ypos, font, size, line))
            self.y -= leading
        if gap_after:
            self._ensure(gap_after)

    def finalize(self):
        if self.cur:
            self.pages.append(self.cur)
            self.cur = []


def maxc(doc, size, bold, x_left):
    usable = doc.W - doc.margin - x_left
    f = 0.56 if bold else 0.52
    return max(8, int(usable / (size * f)))


def wrap(text, max_chars):
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        if not cur:
            if len(w) <= max_chars:
                cur = w
            else:
                while len(w) > max_chars:
                    lines.append(w[:max_chars])
                    w = w[max_chars:]
                cur = w
        else:
            if len(cur) + 1 + len(w) <= max_chars:
                cur = cur + " " + w
            else:
                lines.append(cur)
                cur = w
                if len(w) > max_chars:
                    while len(w) > max_chars:
                        lines.append(w[:max_chars])
                        w = w[max_chars:]
                    cur = w
    if cur:
        lines.append(cur)
    return lines


def bullet_lines(text, max_chars):
    base = wrap(text, max_chars - 2)
    out = []
    for i, ln in enumerate(base):
        out.append(("- " if i == 0 else "  ") + ln)
    return out


def build_pdf(pages):
    W = 595.28
    H = 841.89
    N = len(pages)
    objs = {}
    objs[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join("%d 0 R" % (5 + i) for i in range(N))
    objs[2] = "<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, N)
    objs[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objs[4] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
    for i, page in enumerate(pages):
        pnum = 5 + i
        cnum = 5 + N + i
        objs[pnum] = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
                      "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                      "/Contents %d 0 R >>" % (W, H, cnum))
        stream = ""
        for op in page:
            if op[0] == "LINE":
                _, y, x1, x2, w, g = op
                stream += ("%.2f %.2f %.2f RG %.2f w %.2f %.2f m %.2f %.2f l S\n"
                           % (g, g, g, w, x1, y, x2, y))
            else:
                x, y, font, size, text = op
                stream += ("BT /%s %.2f Tf 1 0 0 1 %.2f %.2f Tm (%s) Tj ET\n"
                           % (font, size, x, y, esc(text)))
        foot = "Page %d of %d" % (i + 1, N)
        fx = (W - len(foot) * 10 * 0.52) / 2
        stream += "BT /F1 9 Tf 1 0 0 1 %.2f 30 Tm (%s) Tj ET\n" % (fx, esc(foot))
        data = stream.encode("latin-1")
        objs[cnum] = "<< /Length %d >>\nstream\n%s\nendstream" % (len(data), stream)
    out = b"%PDF-1.4\n"
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += ("%d 0 obj\n" % num).encode("latin-1")
        out += objs[num].encode("latin-1")
        out += b"\nendobj\n"
    xref_pos = len(out)
    out += ("xref\n0 %d\n" % (len(objs) + 1)).encode("latin-1")
    out += b"0000000000 65535 f \n"
    for num in sorted(objs):
        out += ("%010d 00000 n \n" % offsets[num]).encode("latin-1")
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objs) + 1)).encode("latin-1")
    out += ("startxref\n%d\n" % xref_pos).encode("latin-1") + b"%%EOF"
    return out
C = [
    ("title", "Workforce & Machine Live Monitoring System"),
    ("subtitle", "User Manual and Operating Guide"),
    ("rule", ""),
    ("p", "This manual explains how to install, log in to, and operate the "
          "Workforce & Machine Live Monitoring System: a web-based, "
          "password-protected dashboard for tracking daily workforce numbers "
          "and machine status across your plant."),
    ("p", "It is intended for everyday users (operators, supervisors and "
          "managers) as well as administrators who manage accounts and "
          "access. No programming knowledge is required to use the system."),

    ("h1", "1. Overview"),
    ("p", "The system is a single web application that several people can use "
          "at the same time. Each person signs in with their own username and "
          "password. Whatever one user records (a workforce count, a machine "
          "status, or a production run) is saved to a shared database and "
          "appears live for everyone else."),
    ("h2", "Key features"),
    ("bul", "Live dashboard that automatically refreshes (every 15 seconds) so "
            "the numbers are always current."),
    ("bul", "Daily workforce records: total workforce plus the Metex, CSK, "
            "TopQuality, BestCare and Prestige teams, staff on leave, and "
            "loading staff."),
    ("bul", "Machine status tracking: Running, Break Down, Maintenance and Idle, "
            "with running and out-of-order machine counts and names."),
    ("bul", "An easy update form to create or edit any date and shift."),
    ("bul", "Workforce history that shows the current month by default, with a "
            "month picker to review past or future months."),
    ("bul", "A Production Run Console to start and stop production runs and to "
            "start or stop many machines at once (bulk actions)."),
    ("bul", "Management pages for machines, product groups, products, reports "
            "and users, plus a machine time-summary page for administrators."),
    ("bul", "Responsive design that works on desktop, tablet and mobile."),

    ("h1", "2. System Requirements"),
    ("bul", "Python 3.9 or newer."),
    ("bul", "A modern web browser (Chrome, Edge, Firefox, Safari)."),
    ("bul", "For production: a PostgreSQL database. For quick local testing, no "
            "extra database is needed (SQLite is used automatically)."),
    ("bul", "Optional, for sharing over the internet: the Cloudflare Tunnel "
            "client (cloudflared)."),

    ("h1", "3. Installation and Setup"),
    ("p", "Follow these steps once, on the computer that will host the system."),
    ("h2", "Step 1 - Install dependencies"),
    ("p", "Open a terminal in the project folder and run:"),
    ("p", "    pip install -r requirements.txt"),
    ("h2", "Step 2 - Configure the environment"),
    ("p", "Copy the example configuration and then edit the new .env file:"),
    ("p", "    cp .env.example .env"),
    ("p", "Set at least these three values in .env:"),
    ("bul", "DATABASE_URL - your database connection string. For PostgreSQL use "
            "something like postgresql://postgres:PASSWORD@localhost:5432/"
            "workforce. For a quick local test use sqlite:///workforce.db."),
    ("bul", "DASHBOARD_PASSWORD - the password users type to sign in."),
    ("bul", "SECRET_KEY - a long random string used to keep login sessions "
            "secure."),
    ("h2", "Step 3 - Start the application"),
    ("p", "Run the application with:"),
    ("p", "    python app.py"),
    ("h2", "Step 4 - Open it in a browser"),
    ("p", "Visit http://localhost:5000 . On first start the app creates the "
          "database tables and seeds default users and sample data."),

    ("h1", "4. Logging In"),
    ("p", "Open the application URL. You see a login screen with three fields:"),
    ("bul", "Role - choose the role your account was created with."),
    ("bul", "Username - your account username."),
    ("bul", "Password - your account password."),
    ("p", "The role you select must match the role of the account, otherwise "
          "you will see an error. After a successful login you are taken to "
          "your dashboard."),
    ("h2", "Default accounts"),
    ("p", "When the database is empty, the system creates these starter "
          "accounts (change their passwords after first login):"),
    ("bul", "admin / admin123  (Administrator)"),
    ("bul", "supervisor / super123  (Supervisor)"),
    ("bul", "operator / oper123  (Operator)"),
    ("h2", "Signing out"),
    ("p", "Use the user menu in the top corner to log out. Always log out on "
          "shared computers."),

    ("h1", "5. Roles and Page Access"),
    ("p", "Not every user can see every page. Each user belongs to a role and "
          "has a list of pages they are allowed to open. This is enforced on "
          "the server, so even typing a page address directly will show a "
          "Forbidden (403) message if the user has no access."),
    ("h2", "Available roles"),
    ("bul", "Administrator - full access to every page and settings."),
    ("bul", "General Manager, Operation Manager, Production Manager - all "
            "pages by default; an admin can narrow their access."),
    ("bul", "Supervisor - dashboard, machines, products, groups, reports, run "
            "console, workforce and production runs."),
    ("bul", "Operator - dashboard, machines, run console and production runs."),
    ("bul", "Viewer - read-only: dashboard, machines, products, groups, "
            "reports, workforce and production runs."),
    ("h2", "How access appears in the interface"),
    ("p", "The left navigation sidebar only shows the pages a user is allowed "
          "to open. User Management is visible only to administrators. If a "
          "page is missing for you, ask an administrator to grant access."),

    ("h1", "6. Navigating the Interface"),
    ("h2", "Sidebar navigation"),
    ("p", "The sidebar lists the pages you can access. On a phone it becomes a "
          "slide-out drawer that you open with a menu button."),
    ("h2", "Global date and shift filters"),
    ("p", "The top of the page has Date and Shift (Day / Night) filters. These "
          "apply to the pages that use them, such as the dashboard and the "
          "workforce board, so you can focus on a specific day and shift. A "
          "'Today' control jumps the date back to the current day."),
    ("h2", "Live updates"),
    ("p", "The dashboard refreshes itself automatically every 15 seconds. Most "
          "other pages also re-fetch their data after you make a change, and "
          "many have a Refresh button."),

    ("h1", "7. Page-by-Page Guide"),
    ("h2", "Dashboard (home)"),
    ("p", "The landing page shows live key indicators such as running and "
          "out-of-order machine counts, recent records and summary widgets. It "
          "updates on its own every 15 seconds."),
    ("h2", "Machines"),
    ("p", "View, add, edit and remove machines. For each machine you can set "
          "its status (Running, Break Down, Maintenance or Idle) and link it to "
          "a group and product."),
    ("h2", "Groups and Products"),
    ("p", "Groups organize your products. Use the Groups page to manage product "
          "groups and the Products page to manage the products inside them. "
          "These are used when starting a production run."),
    ("h2", "Reports"),
    ("p", "Shows the history of machine status changes (who changed what and "
          "when), with filters for date range, machine and status. Results can "
          "be exported."),
    ("h2", "Run Console (Production Run Console)"),
    ("p", "This is where production runs are started and stopped:"),
    ("bul", "Start a Production Run: choose a machine, a product group, a "
            "product, and optionally an item name, item code and note, then "
            "click Start Run."),
    ("bul", "Live Runs: currently running productions appear in a grid with a "
            "Stop control."),
    ("bul", "Recent Runs: a table of past and current runs with their status "
            "and run time."),
    ("bul", "Bulk Machine Actions: the 'Start All Machines' and 'Stop All "
            "Machines' buttons open a single form where you pick the new "
            "status and shift and choose which machines (via checkboxes), then "
            "submit. These buttons are visible to anyone who can open the Run "
            "Console page."),
    ("h2", "Workforce Board"),
    ("p", "Used to record and review daily workforce numbers."),
    ("bul", "KPI cards show Total Workforce, Metex, CSK, TopQuality, BestCare, "
            "Prestige, Staff On Leave and Loading Staff. Click a card to see "
            "its breakdown."),
    ("bul", "Update Workforce: pick a Date and Shift (Day or Night), enter the "
            "counts (including staff-on-leave count and names, and loading "
            "staff count and names), then Save."),
    ("bul", "Workforce History: shows the current month by default. Use the "
            "Month picker to switch months and the shift toggle to filter Day "
            "or Night. Records are listed newest first."),
    ("h2", "Production Runs"),
    ("p", "A full list of production runs with their machine, group, product, "
          "item, operator, run time, machine status and run status. Use the "
          "search and filters to find specific runs."),
    ("h2", "User Management (administrators only)"),
    ("p", "Create and edit user accounts: username, password (minimum 4 "
          "characters), display name, role and active/inactive status. You can "
          "also set each user\u0027s page permissions with checkboxes. New users "
          "start with the default permissions for their role; administrators "
          "get every page."),
    ("h2", "Summary (administrators only)"),
    ("p", "A machine-wise time summary showing how long each machine spent "
          "running, idle, in breakdown and in maintenance."),

    ("h1", "8. Recording Daily Workforce Data"),
    ("p", "A typical daily entry:"),
    ("bul", "Open the Workforce Board from the sidebar."),
    ("bul", "Set the Date (use 'Today' for the current day) and choose the "
            "Shift (Day or Night)."),
    ("bul", "Enter the Total Workforce and the sub-team counts: Metex, CSK, "
            "TopQuality, BestCare and Prestige."),
    ("bul", "Enter Staff On Leave (a count and, in the separate field, the "
            "comma-separated names) and Loading Staff (count and names)."),
    ("bul", "Click Save Workforce. The KPI cards update and the record appears "
            "in the history for that date and shift."),
    ("bul", "Review the month using the history Month picker; switch the shift "
            "toggle to see Day versus Night entries."),

    ("h1", "9. Starting and Stopping Production Runs"),
    ("p", "To start a run:"),
    ("bul", "Open the Run Console."),
    ("bul", "Select the Machine, Product Group and Product. Add an item name, "
            "item code and note if needed."),
    ("bul", "Click Start Run. The run appears immediately under Live Runs."),
    ("p", "To stop a run, use the Stop control on its Live Run card or row. The "
          "machine\u0027s open run is closed automatically."),
    ("p", "To act on many machines at once, use Start All Machines or Stop All "
          "Machines, choose the machines in the modal, set the status and "
          "shift, and submit. Production runs are kept in sync with the new "
          "machine status."),

    ("h1", "10. Managing Users and Permissions"),
    ("p", "Administrators manage who can use the system:"),
    ("bul", "Open User Management (visible only to admins)."),
    ("bul", "Add a user with a username, password, display name, role and "
            "active flag."),
    ("bul", "Set page permissions with the checkboxes. The role\u0027s default "
            "pages are filled in automatically; you can allow or restrict "
            "individual pages."),
    ("bul", "Disable a user (set inactive) to block sign-in without deleting "
            "their history."),

    ("h1", "11. Remote Access (Sharing Over the Internet)"),
    ("p", "The app listens on port 5000 on all network interfaces, so other "
          "computers on the same network can reach it directly. To share it "
          "with people anywhere without changing routers, use a Cloudflare "
          "Tunnel:"),
    ("bul", "Start the server (python app.py)."),
    ("bul", "In a second terminal run: cloudflared tunnel --url "
            "http://localhost:5000"),
    ("bul", "Cloudflared prints a public HTTPS address. Share that link and the "
            "DASHBOARD_PASSWORD with your users."),
    ("p", "The random address changes each time you restart the tunnel; create "
          "a named tunnel (free Cloudflare account) for a fixed address. Your "
          "computer must stay on for the link to work."),

    ("h1", "12. Security Notes"),
    ("bul", "Never set DEBUG=True when the app is reachable from the internet; "
            "the debug console allows remote code execution."),
    ("bul", "Use a strong DASHBOARD_PASSWORD and a random SECRET_KEY in .env."),
    ("bul", "Control who can do what through roles and per-page permissions."),
    ("bul", "All users share one database, so recorded data is visible to "
            "everyone who has access to the relevant page."),

    ("h1", "13. Troubleshooting and FAQ"),
    ("bul", "I cannot log in: check that the username and password are correct "
            "and that the selected Role matches the account. Also confirm the "
            "account is active."),
    ("bul", "A page shows Forbidden (403): your account does not have "
            "permission for that page. Ask an administrator to grant it."),
    ("bul", "My change is not showing: wait a few seconds for the automatic "
            "refresh, or press the Refresh button on the page."),
    ("bul", "Where is the data stored: in the database configured by "
            "DATABASE_URL (PostgreSQL for production, or the local "
            "workforce.db SQLite file for testing)."),
    ("bul", "I forgot the admin password: an administrator can reset it through "
            "User Management, or it can be reset directly in the database."),

    ("h1", "14. How It Works (Technical Summary)"),
    ("p", "For readers who want a high-level understanding of the system:"),
    ("bul", "Backend: a Flask application (app.py) handles login, the web pages "
            "and a REST API under /api/*."),
    ("bul", "Database: SQLAlchemy models store Users, Machines, Groups, "
            "Products, DailyRecords (workforce snapshots), ProductionRuns and "
            "MachineLogs (status-change history)."),
    ("bul", "Front end: server-rendered HTML templates with vanilla JavaScript "
            "and CSS; no heavy frameworks are required in the browser."),
    ("bul", "Authentication: session-based login; each request checks the "
            "user\u0027s role and page permissions before showing a page or "
            "returning API data."),
    ("bul", "Live behaviour: the browser periodically re-fetches data (the "
            "dashboard every 15 seconds) and refreshes after each action, so "
            "all viewers see the same up-to-date information."),

    ("h1", "15. Getting Help"),
    ("p", "If something is unclear or not working, check the Troubleshooting "
          "section above first. For account or access issues, contact your "
          "system administrator."),
]


def main():
    doc = Doc()
    for item in C:
        tag = item[0]
        text = item[1] if len(item) > 1 else ""
        if tag == "title":
            lines = wrap(text, maxc(doc, 18, True, doc.margin))
            doc.write_block(lines, doc.margin, "F2", 18, 22, gap_before=0,
                            gap_after=4, center=True)
        elif tag == "subtitle":
            lines = wrap(text, maxc(doc, 12, False, doc.margin))
            doc.write_block(lines, doc.margin, "F1", 12, 16, gap_after=8,
                            center=True)
        elif tag == "rule":
            doc.hline(doc.y, doc.margin, doc.W - doc.margin)
            doc.y -= 12
        elif tag == "h1":
            lines = wrap(text, maxc(doc, 14, True, doc.margin))
            doc.write_block(lines, doc.margin, "F2", 14, 18,
                            gap_before=14, gap_after=6)
        elif tag == "h2":
            lines = wrap(text, maxc(doc, 11.5, True, doc.margin))
            doc.write_block(lines, doc.margin, "F2", 11.5, 15,
                            gap_before=8, gap_after=4)
        elif tag == "p":
            lines = wrap(text, maxc(doc, 10, False, doc.margin))
            doc.write_block(lines, doc.margin, "F1", 10, 13.5, gap_after=5)
        elif tag == "bul":
            x = doc.margin + 14
            mc = maxc(doc, 10, False, x)
            lines = bullet_lines(text, mc)
            doc.write_block(lines, x, "F1", 10, 13.5, gap_after=2)
    doc.finalize()
    data = build_pdf(doc.pages)
    with open(OUT, "wb") as f:
        f.write(data)
    print("Wrote", OUT, "(%d bytes, %d pages)" % (len(data), len(doc.pages)))


if __name__ == "__main__":
    main()
