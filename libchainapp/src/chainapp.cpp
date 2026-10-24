#include "chainapp/chainapp.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QFile>
#include <QHostAddress>
#include <QHttpServer>
#include <QHttpServerRequest>
#include <QHttpServerResponse>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLoggingCategory>
#include <QMap>
#include <QMetaMethod>
#include <QTcpServer>
#include <QTimer>
#include <QUuid>

namespace chainapp {

using Status = QHttpServerResponse::StatusCode;
using Method = QHttpServerRequest::Method;

QString toString(State s)
{
    switch (s) {
    case State::Starting: return QStringLiteral("starting");
    case State::Running:  return QStringLiteral("running");
    case State::Degraded: return QStringLiteral("degraded");
    case State::Paused:   return QStringLiteral("paused");
    case State::Draining: return QStringLiteral("draining");
    case State::Stopping: return QStringLiteral("stopping");
    case State::Error:    return QStringLiteral("error");
    }
    return QStringLiteral("error");
}

namespace {

QByteArray header(const QHttpServerRequest &r, const QByteArray &name)
{
#if QT_VERSION >= QT_VERSION_CHECK(6, 8, 0)
    return r.headers().value(name).toByteArray();
#else
    return r.value(name);
#endif
}

QString nowIso() { return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs); }

QHttpServerResponse problem(Status status, const QString &title, const QString &detail = {})
{
    QJsonObject o{{"type", "about:blank"}, {"title", title}, {"status", int(status)}};
    if (!detail.isEmpty())
        o["detail"] = detail;
    return QHttpServerResponse("application/problem+json",
                               QJsonDocument(o).toJson(QJsonDocument::Compact), status);
}

QString env(const char *name, const QString &def = {})
{
    const QByteArray v = qgetenv(name);
    return v.isEmpty() ? def : QString::fromUtf8(v);
}

struct ConfigEntry {
    QJsonValue value;
    bool runtimeEditable = false;
    bool secret = false;
    std::function<QString(const QJsonValue &)> validator;
};

} // namespace

struct ChainApp::Private {
    Identity id;
    QHttpServer server;
    State state = State::Starting;
    QString message;
    QDateTime since = QDateTime::currentDateTimeUtc();
    QDateTime startedAt = QDateTime::currentDateTimeUtc();
    QList<QPair<QString, std::function<CheckResult()>>> checks;
    QMap<QString, ConfigEntry> config;
    std::function<QJsonObject()> extra;
    qint64 processed = 0, errors = 0, queueDepth = 0;
    QDateTime lastProcessedAt;
    QMap<QString, double> counters, gauges;
    bool stopAfterDrain = false;
    QTimer drainTimeout;
};

ChainApp::ChainApp(const QString &appType, const QString &version, const QString &build, QObject *parent)
    : QObject(parent), d(std::make_unique<Private>())
{
    d->id.appType = appType;
    d->id.version = version;
    d->id.build = build;

    // Vestavěný klíč pro úroveň logování – mění filtr Qt logování za běhu.
    addConfig("logLevel", "INFO", true, false, [](const QJsonValue &v) {
        static const QStringList ok{"DEBUG", "INFO", "WARN", "ERROR"};
        return ok.contains(v.toString()) ? QString() : QStringLiteral("logLevel musí být DEBUG|INFO|WARN|ERROR");
    });
    connect(this, &ChainApp::configChanged, this, [](const QString &key, const QJsonValue &v) {
        if (key != "logLevel")
            return;
        const QString l = v.toString();
        QString rules = "*.debug=false\n";
        if (l == "DEBUG") rules = "*.debug=true\n";
        if (l == "WARN")  rules += "*.info=false\n";
        if (l == "ERROR") rules += "*.info=false\n*.warning=false\n";
        QLoggingCategory::setFilterRules(rules);
    });

    // Pojistka: když aplikace drain nedokončí do 30 s, ukončí se stejně.
    d->drainTimeout.setSingleShot(true);
    d->drainTimeout.setInterval(30000);
    connect(&d->drainTimeout, &QTimer::timeout, this, [this] {
        qWarning("chainapp: drain timeout, forcing finish");
        drainFinished();
    });
}

ChainApp::~ChainApp() = default;

void ChainApp::addReadinessCheck(const QString &name, std::function<CheckResult()> check)
{
    d->checks.append({name, std::move(check)});
}

void ChainApp::addConfig(const QString &key, const QJsonValue &defaultValue, bool runtimeEditable,
                         bool secret, std::function<QString(const QJsonValue &)> validator)
{
    d->config[key] = ConfigEntry{defaultValue, runtimeEditable, secret, std::move(validator)};
}

void ChainApp::setStatusExtra(std::function<QJsonObject()> provider) { d->extra = std::move(provider); }

const Identity &ChainApp::identity() const { return d->id; }
QHttpServer *ChainApp::server() { return &d->server; }
State ChainApp::state() const { return d->state; }
QJsonValue ChainApp::config(const QString &key) const { return d->config.value(key).value; }

void ChainApp::setState(State s, const QString &message)
{
    if (s != d->state)
        d->since = QDateTime::currentDateTimeUtc();
    d->state = s;
    d->message = message;
}

void ChainApp::markProcessed(qint64 n) { d->processed += n; d->lastProcessedAt = QDateTime::currentDateTimeUtc(); }
void ChainApp::markError(qint64 n) { d->errors += n; }
void ChainApp::setQueueDepth(qint64 depth) { d->queueDepth = depth; }
void ChainApp::incCounter(const QString &name, double by) { d->counters[name] += by; }
void ChainApp::setGauge(const QString &name, double value) { d->gauges[name] = value; }

QByteArray ChainApp::correlationId(const QHttpServerRequest &req)
{
    QByteArray id = header(req, CorrelationHeader);
    if (id.isEmpty())
        id = QUuid::createUuid().toString(QUuid::WithoutBraces).toUtf8();
    return id;
}

void ChainApp::drainFinished()
{
    d->drainTimeout.stop();
    if (d->stopAfterDrain) {
        setState(State::Stopping, "stop requested");
        // Odpověď 202 už odešla; ukončíme proces s kódem 0 v dalším cyklu event loopu.
        QTimer::singleShot(200, qApp, [] { QCoreApplication::exit(0); });
    } else {
        setState(State::Paused, "drained");
    }
}

bool ChainApp::start(QString *error)
{
    auto fail = [error](const QString &e) { if (error) *error = e; return false; };

    // 1) Spouštěcí kontrakt od agenta
    Identity &id = d->id;
    id.instanceId    = env("CHAIN_INSTANCE_ID", id.appType.toLower());
    id.chainId       = env("CHAIN_ID");
    id.position      = env("CHAIN_POSITION", "0").toInt();
    id.upstreamUrl   = env("CHAIN_UPSTREAM_URL");
    id.downstreamUrl = env("CHAIN_DOWNSTREAM_URL");
    id.bindAddress   = env("APP_BIND_ADDRESS", "127.0.0.1");
    id.port          = quint16(env("APP_PORT", "0").toUInt());
    id.apiToken      = env("APP_API_TOKEN");
    id.configFile    = env("APP_CONFIG_FILE");
    id.dataDir       = env("APP_DATA_DIR");
    if (id.port == 0)
        return fail("APP_PORT není nastaven");

    // 2) Konfigurace ze souboru od agenta (přepíše výchozí hodnoty)
    auto loadConfigFile = [this]() -> QString {
        if (d->id.configFile.isEmpty())
            return {};
        QFile f(d->id.configFile);
        if (!f.open(QIODevice::ReadOnly))
            return "nelze otevřít " + d->id.configFile;
        const QJsonObject o = QJsonDocument::fromJson(f.readAll()).object();
        for (auto it = o.begin(); it != o.end(); ++it) {
            if (!d->config.contains(it.key()))
                continue;   // neznámé klíče ignorujeme
            auto &e = d->config[it.key()];
            if (e.validator) {
                const QString err = e.validator(it.value());
                if (!err.isEmpty())
                    return it.key() + ": " + err;
            }
            if (e.value != it.value()) {
                e.value = it.value();
                emit configChanged(it.key(), e.value);
            }
        }
        return {};
    };
    if (const QString err = loadConfigFile(); !err.isEmpty())
        return fail(err);
    emit configChanged("logLevel", config("logLevel"));

    // 3) Routy
    auto authorized = [this](const QHttpServerRequest &req) {
        return d->id.apiToken.isEmpty()
            || header(req, "Authorization") == "Bearer " + d->id.apiToken.toUtf8();
    };
    auto runChecks = [this](bool &allOk) {
        QJsonArray arr;
        allOk = true;
        for (const auto &c : d->checks) {
            const CheckResult r = c.second();
            allOk = allOk && r.ok;
            QJsonObject o{{"name", c.first}, {"ok", r.ok}};
            if (!r.detail.isEmpty())
                o["detail"] = r.detail;
            arr.append(o);
        }
        return arr;
    };

    auto &s = d->server;

    s.route("/api/v1/health/live", Method::Get, [] {
        return QHttpServerResponse(QJsonObject{{"status", "ok"}});
    });

    s.route("/api/v1/health/ready", Method::Get, [this, runChecks] {
        bool ok = false;
        QJsonArray checks = runChecks(ok);
        const bool stateOk = d->state == State::Running || d->state == State::Degraded;
        if (!stateOk)
            checks.prepend(QJsonObject{{"name", "state"}, {"ok", false}, {"detail", toString(d->state)}});
        const bool ready = ok && stateOk;
        return QHttpServerResponse(QJsonObject{{"ready", ready}, {"checks", checks}},
                                   ready ? Status::Ok : Status::ServiceUnavailable);
    });

    s.route("/api/v1/info", Method::Get, [this, authorized](const QHttpServerRequest &req) {
        if (!authorized(req))
            return problem(Status::Unauthorized, "Unauthorized");
        const Identity &i = d->id;
        return QHttpServerResponse(QJsonObject{
            {"instanceId", i.instanceId}, {"appType", i.appType}, {"version", i.version},
            {"build", i.build}, {"apiVersion", "1"}, {"chainId", i.chainId}, {"position", i.position},
            {"upstreamUrl", i.upstreamUrl.isEmpty() ? QJsonValue() : QJsonValue(i.upstreamUrl)},
            {"downstreamUrl", i.downstreamUrl.isEmpty() ? QJsonValue() : QJsonValue(i.downstreamUrl)},
            {"pid", qint64(QCoreApplication::applicationPid())},
            {"startedAt", d->startedAt.toString(Qt::ISODateWithMs)},
            {"qtVersion", QString::fromLatin1(qVersion())}});
    });

    s.route("/api/v1/status", Method::Get, [this, authorized, runChecks](const QHttpServerRequest &req) {
        if (!authorized(req))
            return problem(Status::Unauthorized, "Unauthorized");
        bool ok = false;
        QJsonObject o{
            {"instanceId", d->id.instanceId}, {"state", toString(d->state)},
            {"since", d->since.toString(Qt::ISODateWithMs)},
            {"uptimeSec", d->startedAt.secsTo(QDateTime::currentDateTimeUtc())},
            {"message", d->message}, {"checks", runChecks(ok)},
            {"processed", d->processed}, {"errors", d->errors}, {"queueDepth", d->queueDepth},
            {"lastProcessedAt", d->lastProcessedAt.isValid()
                 ? QJsonValue(d->lastProcessedAt.toString(Qt::ISODateWithMs)) : QJsonValue()}};
        if (d->extra)
            o["extra"] = d->extra();
        return QHttpServerResponse(o);
    });

    s.route("/api/v1/metrics", Method::Get, [this, authorized](const QHttpServerRequest &req) {
        if (!authorized(req))
            return problem(Status::Unauthorized, "Unauthorized");
        QByteArray out;
        auto line = [&out](const QString &name, const char *type, double v) {
            out += "# TYPE " + name.toUtf8() + ' ' + type + '\n'
                 + name.toUtf8() + ' ' + QByteArray::number(v, 'g', 15) + '\n';
        };
        line("chain_messages_processed_total", "counter", double(d->processed));
        line("chain_errors_total", "counter", double(d->errors));
        line("chain_queue_depth", "gauge", double(d->queueDepth));
        line("chain_uptime_seconds", "gauge", double(d->startedAt.secsTo(QDateTime::currentDateTimeUtc())));
        for (auto it = d->counters.cbegin(); it != d->counters.cend(); ++it) line(it.key(), "counter", it.value());
        for (auto it = d->gauges.cbegin(); it != d->gauges.cend(); ++it)     line(it.key(), "gauge", it.value());
        return QHttpServerResponse("text/plain; version=0.0.4", out);
    });

    auto configJson = [this] {
        QJsonObject o;
        for (auto it = d->config.cbegin(); it != d->config.cend(); ++it) {
            QJsonObject e{{"value", it->secret ? QJsonValue("***") : it->value},
                          {"runtimeEditable", it->runtimeEditable}};
            if (it->secret)
                e["secret"] = true;
            o[it.key()] = e;
        }
        // Spouštěcí parametry – jen pro čtení, mění je agent restartem.
        o["port"] = QJsonObject{{"value", int(d->id.port)}, {"runtimeEditable", false}};
        o["upstreamUrl"] = QJsonObject{{"value", d->id.upstreamUrl}, {"runtimeEditable", false}};
        o["downstreamUrl"] = QJsonObject{{"value", d->id.downstreamUrl}, {"runtimeEditable", false}};
        return o;
    };

    s.route("/api/v1/config", Method::Get, [authorized, configJson](const QHttpServerRequest &req) {
        if (!authorized(req))
            return problem(Status::Unauthorized, "Unauthorized");
        return QHttpServerResponse(configJson());
    });

    s.route("/api/v1/config", Method::Patch, [this, authorized, configJson](const QHttpServerRequest &req) {
        if (!authorized(req))
            return problem(Status::Unauthorized, "Unauthorized");
        QJsonParseError pe;
        const QJsonDocument doc = QJsonDocument::fromJson(req.body(), &pe);
        if (pe.error != QJsonParseError::NoError || !doc.isObject())
            return problem(Status::BadRequest, "Invalid JSON", pe.errorString());
        const QJsonObject patch = doc.object();
        // Nejdřív vše zvalidovat, pak teprve aplikovat (vše nebo nic).
        for (auto it = patch.begin(); it != patch.end(); ++it) {
            if (!d->config.contains(it.key()))
                return problem(Status::UnprocessableEntity, "Unknown key", it.key());
            const auto &e = d->config[it.key()];
            if (!e.runtimeEditable)
                return problem(Status::UnprocessableEntity, "Key not runtime editable", it.key());
            if (e.validator) {
                const QString err = e.validator(it.value());
                if (!err.isEmpty())
                    return problem(Status::UnprocessableEntity, "Invalid value", it.key() + ": " + err);
            }
        }
        for (auto it = patch.begin(); it != patch.end(); ++it) {
            d->config[it.key()].value = it.value();
            emit configChanged(it.key(), it.value());
        }
        return QHttpServerResponse(configJson());
    });

    s.route("/api/v1/control/<arg>", Method::Post,
            [this, authorized, loadConfigFile](const QString &action, const QHttpServerRequest &req) {
        if (!authorized(req))
            return problem(Status::Unauthorized, "Unauthorized");
        const bool drainHandled = isSignalConnected(QMetaMethod::fromSignal(&ChainApp::drainRequested));
        if (action == "drain" || action == "stop") {
            d->stopAfterDrain = (action == "stop");
            setState(State::Draining, action + " requested");
            if (drainHandled) {
                d->drainTimeout.start();
                emit drainRequested();
            } else {
                QTimer::singleShot(0, this, &ChainApp::drainFinished);
            }
        } else if (action == "pause") {
            setState(State::Paused, "paused by agent");
            emit pauseRequested();
        } else if (action == "resume") {
            if (d->state != State::Paused)
                return problem(Status::Conflict, "Not paused", toString(d->state));
            setState(State::Running);
            emit resumeRequested();
        } else if (action == "reload") {
            if (const QString err = loadConfigFile(); !err.isEmpty())
                return problem(Status::UnprocessableEntity, "Reload failed", err);
            emit reloadRequested();
        } else {
            return problem(Status::NotFound, "Unknown action", action);
        }
        return QHttpServerResponse(QJsonObject{{"accepted", true}, {"action", action},
                                               {"state", toString(d->state)}},
                                   Status::Accepted);
    });

    // 4) Naslouchání (jen na adrese od agenta, výchozí 127.0.0.1)
    const QHostAddress addr(id.bindAddress);
#if QT_VERSION >= QT_VERSION_CHECK(6, 8, 0)
    auto *tcp = new QTcpServer(this);
    if (!tcp->listen(addr, id.port) || !s.bind(tcp))
        return fail(QStringLiteral("nelze naslouchat na %1:%2").arg(id.bindAddress).arg(id.port));
#else
    if (s.listen(addr, id.port) == 0)
        return fail(QStringLiteral("nelze naslouchat na %1:%2").arg(id.bindAddress).arg(id.port));
#endif
    qInfo("chainapp: %s listening on %s:%d (%s)", qPrintable(id.instanceId),
          qPrintable(id.bindAddress), int(id.port), qPrintable(nowIso()));
    return true;
}

} // namespace chainapp
