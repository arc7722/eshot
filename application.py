from flask import Flask, flash, redirect, render_template, render_template_string, request, session, url_for, jsonify, make_response
from flask_session import Session
from passlib.apps import custom_app_context as pwd_context
from functools import wraps
from tempfile import gettempdir
from urllib.parse import urlparse
from flask_mail import Mail, Message
from celery import Celery
import json
import passlib.pwd as pwd
import sqlalchemy
import os
import psycopg2

import time

app = Flask(__name__)

app.config['MAIL_SERVER'] = os.environ.get("MAIL_SERVER")
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASSWORD")

app.config['CELERY_BROKER_URL'] = os.environ.get("REDIS_URL")
app.config['CELERY_RESULT_BACKEND'] = os.environ.get("REDIS_URL")

mail = Mail(app)

celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

url = urlparse(os.environ["DATABASE_URL"])
conn = psycopg2.connect(database = url.path[1:],
                        user     = url.username,
                        password = url.password,
                        host     = url.hostname,
                        port     = url.port)

class SQL(object):
    """Wrap SQLAlchemy to provide a simple SQL API."""

    def __init__(self, url):
        """
        Create instance of sqlalchemy.engine.Engine.

        URL should be a string that indicates database dialect and connection arguments.

        http://docs.sqlalchemy.org/en/latest/core/engines.html#sqlalchemy.create_engine
        """
        try:
            self.engine = sqlalchemy.create_engine(url)
        except Exception as e:
            raise RuntimeError(e)

    def execute(self, text, *multiparams, **params):
        """
        Execute a SQL statement.
        """
        try:

            # bind parameters before statement reaches database, so that bound parameters appear in exceptions
            # http://docs.sqlalchemy.org/en/latest/core/sqlelement.html#sqlalchemy.sql.expression.text
            # https://groups.google.com/forum/#!topic/sqlalchemy/FfLwKT1yQlg
            # http://docs.sqlalchemy.org/en/latest/core/connections.html#sqlalchemy.engine.Engine.execute
            # http://docs.sqlalchemy.org/en/latest/faq/sqlexpressions.html#how-do-i-render-sql-expressions-as-strings-possibly-with-bound-parameters-inlined
            statement = sqlalchemy.text(text).bindparams(*multiparams, **params)
            result = self.engine.execute(str(statement.compile(compile_kwargs={"literal_binds": True})))

            # if SELECT (or INSERT with RETURNING), return result set as list of dict objects
            if result.returns_rows:
                rows = result.fetchall()
                return [dict(row) for row in rows]

            # if INSERT, return primary key value for a newly inserted row
            elif result.lastrowid is not None:
                return result.lastrowid

            # if DELETE or UPDATE (or INSERT without RETURNING), return number of rows matched
            else:
                return result.rowcount

        # if constraint violated, return None
        except sqlalchemy.exc.IntegrityError:
            return None

        # else raise error
        except Exception as e:
            raise RuntimeError(e)



db = SQL(os.environ["DATABASE_URL"])

app.config["SESSION_FILE_DIR"] = gettempdir()
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)


def login_required(f):
    """
    Decorate routes to require login.

    http://flask.pocoo.org/docs/0.11/patterns/viewdecorators/
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("login", next=request.url))
        elif session.get("user_id") == 4:
            return redirect(url_for("marketing"))
        return f(*args, **kwargs)
    return decorated_function
    
def aux_login_required(f):
    """
    Decorate routes to require login.

    http://flask.pocoo.org/docs/0.11/patterns/viewdecorators/
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("login"))        
        return f(*args, **kwargs)
    return decorated_function


def obfuscate_id(user_id):
    userid = user_id << 16
    userid = userid ^ int(os.environ.get("ID_OBF_KEY"))
    return userid

def deobfuscate_id(obf_id):
    userid = obf_id ^ int(os.environ.get("ID_OBF_KEY"))
    userid = userid >> 16
    return userid

def eshot_from_desc(eshot_desc):
    eshot = []
    counter = 0
    while counter < 8:
        booking_term = "booking" + str(counter)
        price_term  = "price" + str(counter)
        booking_id = eshot_desc[0][booking_term]
        price = eshot_desc[0][price_term]

        if booking_id == 0 or booking_id == '0':
            break

        booking = db.execute("SELECT bookings.date, bookings.course, courses.name, courses.type, LEFT(courses.description, 200) AS description, to_char(EXTRACT(day FROM CAST(bookings.date as DATE)), '99') as daynum, to_char(CAST(bookings.date as DATE), 'Month') AS month, to_char(CAST(bookings.date as DATE), 'yyyy') AS year FROM bookings INNER JOIN courses on bookings.course = courses.id WHERE bookings.id = :bookingid", bookingid = booking_id)
        
        eshot_booking = {"booking_id":   booking_id,
                        "booking_date":  booking[0]['date'],
                        "daynum":        booking[0]['daynum'],
                        "month":         booking[0]['month'],
                        "year":          booking[0]['year'],
                        "course_id":     booking[0]['course'],
                        "course_type":   booking[0]['type'],
                        "course_name":   booking[0]['name'],
                        "course_desc":   booking[0]['description'],                                
                        "booking_price": price}

        eshot.append(eshot_booking)            
        counter += 1
        
    return eshot    


@celery.task(bind=True)
def send_eshot_task(self, eshot_params, unsubscribe_url):
    eshot_id       = eshot_params["eshot_id"]
    dont_send_list = eshot_params["dont_send_list"]
    
    recipient_list = list()
    if eshot_params["recipient_list"] == "test":
        recipient_list = db.execute("SELECT id, contact, email FROM marketing_test WHERE consent = 1 ORDER BY id")
    else:
        recipient_list = db.execute("SELECT id, contact, email FROM marketing WHERE consent = 1 ORDER BY id")
    
    eshot_desc = db.execute("SELECT id, subject, booking0, price0, booking1, price1, booking2, price2, booking3, price3, booking4, price4, booking5, price5, booking6, price6, booking7, price7 FROM eshots WHERE id = :id",
                          id = eshot_id)

    eshot = eshot_from_desc(eshot_desc) 
        
    total = len(recipient_list) - len(dont_send_list)
    counter = 0    
    for recipient in recipient_list:
        if recipient["id"] not in dont_send_list:
            print(recipient["id"])
            print(recipient["email"])
            
            obf_id = obfuscate_id(recipient["id"])            
            recipient_unsubscribe_url = unsubscribe_url + str(obf_id)
            
            if eshot_params["recipient_list"] == "test":
                recipient_unsubscribe_url = "#"
            else:
                obf_id = obfuscate_id(recipient["id"])            
                recipient_unsubscribe_url = unsubscribe_url + str(obf_id)
                
            counter += 1
            self.update_state(state='PROGRESS',
                              meta={'current':   counter,
                                    'total':     total,
                                    'recipient': recipient["email"],
                                    'status':    'ongoing'})    
                
            with app.app_context():
                msg = Message(subject    = eshot_desc[0]["subject"], 
                              sender     = "claire.scott@skillsgen.com",
                              reply_to   = "karen.reinolds@skillsgen.com",
                              recipients = [recipient["email"]])
                msg.html = render_template("email.html", 
                                           eshot = eshot, 
                                           subject = eshot_desc[0]['subject'], 
                                           unsubscribe_url = recipient_unsubscribe_url)            
                try:
                    mail.send(msg)
                except Exception as error:                    
                    self.update_state(state='PROGRESS',
                              meta={'current':   counter,
                                    'total':     total,
                                    'recipient': "FAILED",
                                    'status':    'ongoing'}) 
                    time.sleep(2)
                    print(error)

    db.execute("UPDATE eshots SET lastsent = CURRENT_DATE WHERE id = :eshot_id", eshot_id = eshot_id)
    
    return {'current': counter,
            'total':   total,
            'status':  'completed'}


@app.route("/login", methods=["GET", "POST"])
def login(message=""):
    if request.method == "POST":
        if not request.form.get("username"):
            return render_template("login.html", message = "Username required.")
        elif not request.form.get("password"):
            return render_template("login.html", message = "Password required.")
        
        ver = db.execute("SELECT * FROM users WHERE username = :username", username = request.form.get("username"))
        if len(ver) != 1 or not pwd_context.verify(request.form.get("password"), ver[0]["hash"]):
            return render_template("login.html", message = "Incorrect password or nonexistant Username")
        
        else:
            session["user_id"] = ver[0]["id"]
            return redirect(url_for("index"))      
    else:
        return render_template("login.html")

    
@app.route("/logout")
def logout():
    
    session.clear()

    return redirect(url_for("login"))


@app.route("/", methods = ['GET','POST'])
@aux_login_required
def index(message=""):    
    if request.method == "POST":
        if request.form.get("search") != None:
            if request.form.get("search") != "":
                q = request.form.get("search")
                q = "%%" + q + "%%"
                
                if request.form.get("onlyflagged") == '0':
                    searchresults = db.execute("SELECT * FROM marketing WHERE (business ILIKE :q) OR (contact ILIKE :q) OR (email ILIKE :q) OR (phone ILIKE :q) ORDER BY id DESC", q = q)
                else:
                    searchresults = db.execute("SELECT * FROM marketing WHERE ((business ILIKE :q) OR (contact ILIKE :q) OR (email ILIKE :q) OR (phone ILIKE :q)) AND (flag = true) ORDER BY id DESC", q = q)
                    
                return jsonify(searchresults)
            else:
                if request.form.get("onlyflagged") == '0':
                    clients = db.execute("SELECT * FROM marketing ORDER BY id DESC")
                else:
                    clients = db.execute("SELECT * FROM marketing WHERE flag = true ORDER BY id DESC")
                return jsonify(clients)
        
        elif request.form.get("delete") != None:
            db.execute("DELETE FROM marketing WHERE id = :clientid",
                      clientid = request.form.get("clientid"))
            
            return jsonify("success")
        
        elif request.form.get("flag") != None:
            db.execute("UPDATE marketing SET flag = NOT flag WHERE id = :clientid",
                        clientid = request.form.get("flag"))
            
            return jsonify(request.form.get("flag"))
        
        elif request.form.get("clientid") != None:
            db.execute("UPDATE marketing SET business = :business, contact = :contact, email = :email, phone = :phone, notes = :notes WHERE id = :clientid",
                      business = request.form.get("client"),
                      contact  = request.form.get("contact"),
                      email    = request.form.get("email"),
                      phone    = request.form.get("phone"),
                      notes    = request.form.get("notes"),
                      clientid = request.form.get("clientid"))
            
            return jsonify(request.form.get("clientid"))
            
        elif request.form.get("clientid") == None:
            newclientid = db.execute("INSERT INTO marketing (business, contact, email, phone, notes, userid)                                    VALUES(:business, :contact, :email, :phone, :notes, :userid)",
                                    business = request.form.get("client"),
                                    contact  = request.form.get("contact"),
                                    email    = request.form.get("email"),
                                    phone    = request.form.get("phone"),
                                    notes    = request.form.get("notes"),
                                    userid   = session["user_id"])
            
            return jsonify(newclientid)
    else:
        return render_template("index.html")

@app.route("/new_eshot")
@aux_login_required
def new_eshot():
    return render_template("new_eshot.html")
     
    
@app.route("/send", methods=["GET", "POST"])
@aux_login_required
def send():
    if request.method == "POST":
        if request.form.get("list") == "test":
            recipient_list = db.execute("SELECT id, contact, email FROM marketing_test WHERE consent = 1 ORDER BY id")
        else:
            recipient_list = db.execute("SELECT id, contact, email FROM marketing WHERE consent = 1 ORDER BY id")
        
        return jsonify(recipient_list)
            
    elif request.args.get("eshotid") != None:
        eshot_desc = db.execute("SELECT id, subject, booking0, price0, booking1, price1, booking2, price2, booking3, price3, booking4, price4, booking5, price5, booking6, price6, booking7, price7 FROM eshots WHERE id = :id",
                          id = request.args.get("eshotid"))
        
        eshot = eshot_from_desc(eshot_desc)
            
        return render_template("send.html", eshot_id = request.args.get("eshotid"), eshot = eshot, subject = eshot_desc[0]['subject'])
    
    else:
        return "eshot id not found"

    
@app.route("/eshots", methods=["GET", "POST"])
@aux_login_required
def eshots():
    eshots = db.execute("SELECT id, to_char(EXTRACT(day FROM timestamp), '99') as daynum, to_char(timestamp, 'Month') AS month, to_char(timestamp, 'yyyy') AS year, to_char(lastsent, 'dd-mm-yy') AS lastsent, subject FROM eshots")
    
    return render_template("eshots.html", eshots = eshots)
    
    
@app.route("/email", methods=["GET"])
@aux_login_required
def email():
    if request.args.get("eshotid") != None:
        eshot_desc = db.execute("SELECT id, subject, booking0, price0, booking1, price1, booking2, price2, booking3, price3, booking4, price4, booking5, price5, booking6, price6, booking7, price7 FROM eshots WHERE id = :id",
                              id = request.args.get("eshotid"))

        eshot = eshot_from_desc(eshot_desc)
        unsubscribe_url = "https://skillsgen-eshot.herokuapp.com" + url_for('unsubscribe', identifier = "") + "27332081"
        print(unsubscribe_url)
        return render_template("email.html", eshot = eshot, subject = eshot_desc[0]['subject'], unsubscribe_url = unsubscribe_url)
    else:
        return("need eshotid")
    
    
@app.route("/search", methods=["POST"])
@aux_login_required
def search():
    if request.method == "POST":
        if request.form.get("count"):
            count = request.form.get("count")
            
            eshot = []
            for i in range(int(count)):
                booking_id_term = "id" + str(i)
                price_term      = "price" + str(i)
                booking_id = request.form.get(booking_id_term)                
                price      = request.form.get(price_term)
                
                course = db.execute("SELECT bookings.date, bookings.course, courses.name, courses.type, LEFT(courses.description, 150) AS description, to_char(EXTRACT(day FROM CAST(bookings.date as DATE)), '99') as daynum, to_char(CAST(bookings.date as DATE), 'Month') AS month, to_char(CAST(bookings.date as DATE), 'yyyy') AS year FROM bookings INNER JOIN courses on bookings.course = courses.id WHERE bookings.id = :bookingid", bookingid = booking_id)
                
                eshot_course = {"booking_id":    booking_id,
                                "booking_date":  course[0]['date'],
                                "daynum":        course[0]['daynum'],
                                "month":         course[0]['month'],
                                "year":          course[0]['year'],
                                "course_id":     course[0]['course'],
                                "course_type":   course[0]['type'],
                                "course_name":   course[0]['name'],
                                "course_desc":   course[0]['description'],
                                "booking_price": price}
                eshot.append(eshot_course)
            
            return jsonify(eshot)            
            
        elif request.form.get("term"):
            q = request.form.get("term")
            q = "%%" + q + "%%"
            courses = db.execute("SELECT id, name FROM courses WHERE (name ILIKE :q)", q=q)
        
            return jsonify(courses)
        
        elif request.form.get("course_id"):
            courseid = request.form.get("course_id")
            dates = db.execute("SELECT id, date FROM bookings WHERE course = :courseid AND to_date(date, 'YYYY-MM-DD') >= CURRENT_DATE ORDER BY date", courseid = courseid)
            
            return jsonify(dates)    
        
        else:
            return("something went wrong")
    
    else: 
        return("you shouldn't be here")
    
    
@app.route("/save", methods=["POST"])
@aux_login_required
def save():
    eshot = request.get_json()
    bookings = [0, 0, 0, 0, 0, 0, 0, 0]
    prices   = [0, 0, 0, 0, 0, 0, 0, 0]
    
    length = 8
    if len(eshot["eshot_courses"]) < 8:
        length = len(eshot["eshot_courses"])
        
    for i in range(length):
        bookings[i] = eshot["eshot_courses"][i]["id"]
        prices[i]   = eshot["eshot_courses"][i]["price"]
        
    id = db.execute("INSERT INTO eshots (subject, booking0, price0, booking1, price1, booking2, price2, booking3, price3, booking4, price4, booking5, price5, booking6, price6, booking7, price7) VALUES (:subject, :booking0, :price0, :booking1, :price1, :booking2, :price2, :booking3, :price3, :booking4, :price4, :booking5, :price5, :booking6, :price6, :booking7, :price7)", 
                    subject  = eshot["subject"],
                    booking0 = bookings[0],
                    price0   = prices[0],
                    booking1 = bookings[1],
                    price1   = prices[1],
                    booking2 = bookings[2],
                    price2   = prices[2],          #can you tell we pay for our database storage by row.
                    booking3 = bookings[3],
                    price3   = prices[3],
                    booking4 = bookings[4],
                    price4   = prices[4],
                    booking5 = bookings[5],
                    price5   = prices[5],
                    booking6 = bookings[6],
                    price6   = prices[6],
                    booking7 = bookings[7],
                    price7   = prices[7])
    
    return("ThumbsUp")


@app.route("/send_eshot", methods=["POST"])
@aux_login_required
def send_eshot():
    eshot_params = request.get_json()
    unsubscribe_url = "https://skillsgen-eshot.herokuapp.com" + url_for('unsubscribe', identifier = "")
    
    task_id = send_eshot_task.apply_async(args=[eshot_params, unsubscribe_url])
    
    return str(task_id)


@app.route('/send_progress', methods=["POST"])
@aux_login_required
def send_progress():
    task_id = request.form.get("task_id")
    print(task_id)
    task = send_eshot_task.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state':   task.state,
            'current': 0,
            'total':   1,
            'status':  'Eshot has not started yet...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state':     task.state,
            'current':   task.info.get('current', 0),
            'total':     task.info.get('total', 1),
            'status':    task.info.get('status', ''),
            'recipient': task.info.get('recipient', '')
        }
        if 'result' in task.info:
            response['result'] = task.info['result']
    else:
        # something went wrong in the background job
        response = {
            'state':   task.state,
            'current': 1,
            'total':   1,
            'status':  str(task.info),  # this is the exception raised
        }
        time.sleep(3)
    return jsonify(response)


@app.route("/unsubscribe", methods=["GET"])
def unsubscribe(): 
    if request.args.get("identifier") != None:
        try:
            obf_id = int(request.args.get("identifier"))
            user_id = deobfuscate_id(obf_id)            
            db.execute("UPDATE marketing SET consent = 0 WHERE id = :user_id", user_id = user_id)
            
            return "You will no longer receive our marketing emails. Thankyou."
        
        except:
            return "Invalid identifier"
    else:
        return "Invalid identifier"
    
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port,debug=False)
