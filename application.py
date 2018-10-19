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

app = Flask(__name__)

app.config['MAIL_SERVER'] = os.environ.get("MAIL_SERVER")
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASSWORD")
mail = Mail(app)

app.config['CELERY_BROKER_URL'] = os.environ.get("REDIS_URL")
app.config['CELERY_RESULT_BACKEND'] = os.environ.get("REDIS_URL")

celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

url = urlparse(os.environ["DATABASE_URL"])
conn = psycopg2.connect(
 database=url.path[1:],
 user=url.username,
 password=url.password,
 host=url.hostname,
 port=url.port
)

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

@celery.task
def background_task(arg1, arg2):
    print("running background task")


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
                      contact = request.form.get("contact"),
                      email = request.form.get("email"),
                      phone = request.form.get("phone"),
                      notes = request.form.get("notes"),
                      clientid = request.form.get("clientid"))
            
            return jsonify(request.form.get("clientid"))
            
        elif request.form.get("clientid") == None:
            newclientid = db.execute("INSERT INTO marketing (business, contact, email, phone, notes, userid)                                    VALUES(:business, :contact, :email, :phone, :notes, :userid)",
                                    business = request.form.get("client"),
                                    contact = request.form.get("contact"),
                                    email = request.form.get("email"),
                                    phone = request.form.get("phone"),
                                    notes = request.form.get("notes"),
                                    userid = session["user_id"])
            
            return jsonify(newclientid)
    else:
        task = background_task.delay(1,2)
        return render_template("index.html")

@app.route("/eshot")
def eshot():
    return render_template("eshot.html")
    
@app.route("/search", methods=["POST"])
def search():
    if request.method == "POST":
        q = request.form.get("term")
        q = "%%" + q + "%%"
        courses = db.execute("SELECT id, name FROM courses WHERE (name ILIKE :q)", q=q)
        
        results = list()
        for course in courses:
            coursedict = dict()
            coursedict["id"] = course["id"]
            coursedict["name"] = course["name"]
            coursedict["dates"] = list()
                        
            dates = db.execute("SELECT date FROM bookings WHERE course = :course_id AND CAST(date as DATE) < CURRENT_DATE",
                               course_id = course["id"])
            
            for date in dates:
                coursedict["dates"].append(date["date"])
            
            results.append(coursedict)
        
        return jsonify(results)
    
    

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port,debug=True)
