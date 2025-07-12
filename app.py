from flask import Flask, render_template, request, redirect, session, url_for, flash
from datetime import datetime, timedelta
import os
import mysql.connector
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import make_response
from xhtml2pdf import pisa
from io import BytesIO


# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your_secret_key')

# MySQL configuration from .env
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME'),
    'port': int(os.getenv('DB_PORT', 3306)), 
}

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

@app.route('/')
def index():
    return render_template('home.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')

        if not name or not email or not password:
            flash('All fields are required.')
            return redirect(url_for('register'))

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO users (name, email, password) VALUES (%s, %s, %s)', (name, email, password))
            conn.commit()
            return redirect(url_for('login'))
        except mysql.connector.IntegrityError:
            flash('Email already exists!', 'error')
        except Exception as e:
            flash('Unexpected error occurred.')
            print(f"DEBUG: {e}")
        finally:
            cursor.close()
            conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute('SELECT * FROM users WHERE email = %s AND password = %s', (email, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        if user:
            session['user_id'] = user['user_id']
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials, please enter a valid password.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # ✅ Auto-expire the user's passes
    today = datetime.today().date()
    cursor.execute('''
        UPDATE bus_passes
        SET status = 'expired'
        WHERE user_id = %s AND valid_until < %s AND status = 'live'
    ''', (session['user_id'], today))
    conn.commit()

    # ✅ Continue with original logic
    cursor.execute('SELECT * FROM users WHERE user_id = %s', (session['user_id'],))
    user = cursor.fetchone()
    cursor.execute('SELECT * FROM bus_passes WHERE user_id = %s ORDER BY pass_id DESC LIMIT 1', (session['user_id'],))
    bus_pass = cursor.fetchone()

    cursor.close()
    conn.close()

    bus_pass_id = bus_pass['pass_id'] if bus_pass else None
    return render_template('dashboard.html', user=user, bus_pass=bus_pass, bus_pass_id=bus_pass_id)

@app.route('/apply', methods=['GET', 'POST'])
def apply():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute('SELECT * FROM routes')
    routes = cursor.fetchall()

    if request.method == 'POST':
        route_id = request.form['route_id']
        user_id = session['user_id']
        issue_date = datetime.now().date()
        validity_days = int(request.form['validity'])
        valid_until = issue_date + timedelta(days=validity_days)

        cursor.execute('''SELECT * FROM bus_passes 
                          WHERE user_id = %s AND route_id = %s AND valid_until >= %s 
                          AND status IN ('pending', 'approved')''',
                       (user_id, route_id, issue_date))
        existing_pass = cursor.fetchone()

        if existing_pass:
            cursor.close()
            conn.close()
            flash('You have already applied or have an active pass for this route.')
            return redirect(url_for('apply'))

        cursor.execute('''INSERT INTO bus_passes (user_id, route_id, issue_date, valid_until, status)
                          VALUES (%s, %s, %s, %s, %s)''',
                       (user_id, route_id, issue_date, valid_until, 'pending'))
        conn.commit()
        bus_pass_id = cursor.lastrowid
        cursor.close()
        conn.close()
        return redirect(url_for('payment', bus_pass_id=bus_pass_id))

    cursor.close()
    conn.close()
    return render_template('apply.html', routes=routes)

@app.route('/payment/<int:bus_pass_id>', methods=['GET', 'POST'])
def payment(bus_pass_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute('SELECT issue_date, valid_until FROM bus_passes WHERE pass_id = %s AND user_id = %s',
                   (bus_pass_id, session['user_id']))
    bus_pass = cursor.fetchone()

    if not bus_pass:
        cursor.close()
        conn.close()
        flash("Bus pass not found.")
        return redirect(url_for('dashboard'))

    issue_date = bus_pass['issue_date']
    valid_until = bus_pass['valid_until']
    validity_days = (valid_until - issue_date).days if isinstance(issue_date, datetime) else (
        datetime.strptime(str(valid_until), "%Y-%m-%d") - datetime.strptime(str(issue_date), "%Y-%m-%d")).days

    price = 100
    if validity_days == 1:
        price = 50
    elif validity_days == 30:
        price = 500
    elif validity_days == 365:
        price = 1800

    if request.method == 'POST':
        # Insert payment
        cursor.execute('''INSERT INTO payments (user_id, bus_pass_id, amount, payment_date)
                          VALUES (%s, %s, %s, %s)''',
                       (session['user_id'], bus_pass_id, price, datetime.now().strftime('%Y-%m-%d')))

        # Update status to live
        cursor.execute('''UPDATE bus_passes SET status = 'live' WHERE pass_id = %s''', (bus_pass_id,))

        # Fetch user and pass details
        cursor.execute('''
            SELECT u.name, u.email, r.route_name, r.origin, r.destination, bp.issue_date, bp.valid_until 
            FROM bus_passes bp
            JOIN users u ON bp.user_id = u.user_id
            JOIN routes r ON bp.route_id = r.route_id
            WHERE bp.pass_id = %s
        ''', (bus_pass_id,))
        details = cursor.fetchone()

        # Compose email
        subject = "Your Bus Pass is Now Live"
        body = f"""
        <p>Dear {details['name']},</p>
        <p>Your bus pass has been successfully activated.</p>
        <ul>
          <li><strong>Route:</strong> {details['route_name']}</li>
          <li><strong>From:</strong> {details['origin']}</li>
          <li><strong>To:</strong> {details['destination']}</li>
          <li><strong>Issue Date:</strong> {details['issue_date']}</li>
          <li><strong>Valid Until:</strong> {details['valid_until']}</li>
        </ul>
        <p>You can now start using your pass.</p>
        <p><a href="{url_for('view_pass', _external=True)}">Click here to view your pass</a></p>
        """

        send_email(details['email'], subject, body)

        conn.commit()
        cursor.close()
        conn.close()
        return redirect(url_for('view_pass'))

    # GET request – render payment form
    cursor.close()
    conn.close()
    return render_template('payment.html', bus_pass_id=bus_pass_id, price=price)

def send_email(to_email, subject, body):
    from_email = os.getenv('EMAIL_USER')
    password = os.getenv('EMAIL_PASS')

    message = MIMEMultipart()
    message['From'] = from_email
    message['To'] = to_email
    message['Subject'] = subject

    message.attach(MIMEText(body, 'html'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(from_email, password)
        server.send_message(message)
        server.quit()
        print("✅ Email sent successfully.")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")


@app.route('/view_passes')
def view_pass():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    today = datetime.today().date()
    cursor.execute('''
        UPDATE bus_passes 
        SET status = 'expired'
        WHERE valid_until < %s AND status = 'live'
    ''', (today,))
    conn.commit()

    # Fetch passes for this user
    cursor.execute('''
        SELECT bp.pass_id, bp.issue_date, bp.valid_until, bp.status,
               r.route_name, r.origin, r.destination
        FROM bus_passes bp
        JOIN routes r ON bp.route_id = r.route_id  
        WHERE bp.user_id = %s
    ''', (session['user_id'],))
    passes = cursor.fetchall()

    cursor.close()
    conn.close()

    # Pass today's date to the template
    return render_template('view.html', all_passes=passes, current_date=today)

@app.route('/download_pass/<int:pass_id>')
def download_pass(pass_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Fetch pass and route details
    cursor.execute('''
        SELECT bp.*, r.route_name, r.origin, r.destination, u.name, u.email
        FROM bus_passes bp
        JOIN routes r ON bp.route_id = r.route_id
        JOIN users u ON bp.user_id = u.user_id
        WHERE bp.pass_id = %s AND bp.user_id = %s
    ''', (pass_id, session['user_id']))
    bus_pass = cursor.fetchone()

    cursor.close()
    conn.close()

    if not bus_pass:
        flash("Pass not found.")
        return redirect(url_for('dashboard'))

    # Render template to HTML
    html = render_template("pass_pdf.html", bus_pass=bus_pass)

    # Generate PDF
    result = BytesIO()
    pdf = pisa.pisaDocument(BytesIO(html.encode("UTF-8")), result)

    if pdf.err:
        return "PDF generation failed"

    response = make_response(result.getvalue())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = "attachment; filename=bus_pass.pdf"
    return response

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        with get_db_connection() as conn:
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute('SELECT * FROM admins WHERE username = %s AND password = %s', (username, password))
                admin = cursor.fetchone()
                cursor.fetchall()  # Optional: clear any unread rows

        if admin:
            session['admin_id'] = admin['admin_id']
            return redirect(url_for('admin_dashboard'))
        else:
            return render_template('admin_login.html', error='Invalid credentials')

    return render_template('admin_login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'admin_id' not in session:
        return redirect(url_for('admin_login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Auto-expire all "live" passes whose validity is over
    today = datetime.today().date()
    cursor.execute('''
        UPDATE bus_passes 
        SET status = 'expired'
        WHERE valid_until < %s AND status = 'live'
    ''', (today,))
    conn.commit()

    # Fetch all users
    cursor.execute('SELECT * FROM users')
    users = cursor.fetchall()

    # Fetch all passes with joined info
    cursor.execute('''
    SELECT p.pass_id, p.user_id, p.issue_date, p.valid_until, p.status,
           u.name AS user_name, u.email,
           r.route_name, r.origin, r.destination,
           pay.amount, pay.payment_date
    FROM bus_passes p
    JOIN users u ON p.user_id = u.user_id
    JOIN routes r ON p.route_id = r.route_id
    LEFT JOIN payments pay ON p.pass_id = pay.bus_pass_id
''')
    passes = cursor.fetchall()

    cursor.close()
    conn.close()

    current_date = datetime.today().date()
    return render_template('admin_dashboard.html', users=users, passes=passes, current_date=current_date)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    return redirect(url_for('admin_login'))

if __name__ == '__main__':
    app.run(debug=True)