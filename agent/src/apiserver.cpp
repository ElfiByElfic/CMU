#include "apiserver.h"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QHostAddress>
#include <QHttpServerRequest>
#include <QHttpServerResponse>
#include <QJsonDocument>
#include <QTcpServer>
#include <QUrlQuery>

using Status = QHttpServerResponse::StatusCode;
using Method = QHttpServerRequest::Method;

namespace {

QByteArray header(const QHttpServerRequest &r, const QByteArray &name)
{
#if QT_VERSION >= QT_VERSION_CHECK(6, 8, 0)
    return r.headers().value(name).toByteArray();
#else
    return r.value(name);
#endif
}

QHttpServerResponse problem(Status status, const QString &title, const QString &detail = {},
                            const QStringList &errors = {})
{
    QJsonObject o{{"type", "about:blank"}, {"title", title}, {"status", int(status)}};
    if (!detail.isEmpty()) o["detail"] = detail;
    if (!errors.isEmpty()) o["errors"] = QJsonArray::fromStringList(errors);
    return QHttpServerResponse("application/problem+json",
                               QJsonDocument(o).toJson(QJsonDocument::Compact), status);
}

// Převod chybového kódu z ChainManager na HTTP odpověď.
QHttpServerResponse fromError(const QString &err)
{
    if (err == "not-found")      return problem(Status::NotFound, "Not found");
    if (err == "unknown-action") return problem(Status::NotFound, "Unknown action");
    if (err.startsWith("busy:")) return problem(Status::Conflict, "Operation in progress", err.mid(5));
    return problem(Status::UnprocessableEntity, "Rejected", err);
}

bool parseObject(const QHttpServerRequest &req, QJsonObject *out, QString *error)
{
    QJsonParseError pe;
    const QJsonDocument doc = QJsonDocument::fromJson(req.body(), &pe);
    if (pe.error != QJsonParseError::NoError || !doc.isObject()) {
        *error = pe.errorString();
        return false;
    }
    *out = doc.object();
    return true;
}

} // namespace

// Ověření role; při nedostatečné roli rovnou vrátí 401/403 z route lambdy.
#define REQUIRE(needed)                                                                    \
    do {                                                                                   \
        const Role r_ = role(req);                                                         \
        if (r_ < (needed))                                                                 \
            return problem(r_ == Role::None ? Status::Unauthorized : Status::Forbidden,    \
                           r_ == Role::None ? "Unauthorized" : "Forbidden");               \
    } while (0)

ApiServer::ApiServer(const AgentConfig &cfg, ChainManager *chains, Catalog *catalog, Operations *ops,
                     QObject *parent)
    : QObject(parent), cfg_(cfg), chains_(chains), catalog_(catalog), ops_(ops)
{
    setupRoutes();
}

ApiServer::Role ApiServer::role(const QHttpServerRequest &req) const
{
    const QByteArray auth = header(req, "Authorization");
    if (!auth.startsWith("Bearer "))
        return Role::None;
    const QString token = QString::fromUtf8(auth.mid(7));
    if (cfg_.controlTokens.contains(token)) return Role::Control;
    if (cfg_.readTokens.contains(token))    return Role::Read;
    return Role::None;
}

void ApiServer::audit(const QHttpServerRequest &req, const QString &what) const
{
    // Do auditu nikdy neukládáme token, jen jeho otisk.
    const QByteArray token = header(req, "Authorization").mid(7);
    const QString who = QString::fromLatin1(QCryptographicHash::hash(token, QCryptographicHash::Sha256).toHex().left(8));
    QDir().mkpath(cfg_.logDir);
    QFile f(cfg_.logDir + "/audit.log");
    if (f.open(QIODevice::Append | QIODevice::Text))
        f.write(QStringLiteral("%1 token:%2 %3 %4\n")
                    .arg(QDateTime::currentDateTimeUtc().toString(Qt::ISODate), who,
                         req.url().path(), what).toUtf8());
}

QJsonObject ApiServer::info() const
{
    return {{"vmName", cfg_.vmName}, {"environment", cfg_.environment},
            {"agentVersion", QCoreApplication::applicationVersion()},
            {"startedAt", startedAt_.toString(Qt::ISODate)},
            {"portRange", QJsonArray{cfg_.portMin, cfg_.portMax}}};
}

void ApiServer::setupRoutes()
{
    auto &s = server_;
    const QString P = "/agent/v1";

    auto opAccepted = [this](const QString &opId) {
        return QHttpServerResponse(ops_->get(opId), Status::Accepted);
    };

    // --- čtení -------------------------------------------------------------
    s.route(P + "/health", Method::Get, [] {
        return QHttpServerResponse(QJsonObject{{"status", "ok"}});
    });

    s.route(P + "/info", Method::Get, [this](const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        return QHttpServerResponse(info());
    });

    s.route(P + "/overview", Method::Get, [this](const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        return QHttpServerResponse(QJsonObject{
            {"vm", info()}, {"state", chains_->vmState()}, {"chains", chains_->chainsJson()},
            {"generatedAt", QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)}});
    });

    s.route(P + "/catalog", Method::Get, [this](const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        catalog_->rescan();
        return QHttpServerResponse(catalog_->toJson());
    });

    s.route(P + "/chains", Method::Get, [this](const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        return QHttpServerResponse(chains_->chainsJson());
    });

    s.route(P + "/chains/<arg>", Method::Get, [this](const QString &id, const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        if (!chains_->hasChain(id))
            return problem(Status::NotFound, "Chain not found", id);
        return QHttpServerResponse(chains_->chainJson(id));
    });

    s.route(P + "/apps/<arg>", Method::Get, [this](const QString &id, const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        AppInstance *a = chains_->app(id);
        if (!a)
            return problem(Status::NotFound, "Instance not found", id);
        return QHttpServerResponse(a->toJson());
    });

    s.route(P + "/apps/<arg>/logs", Method::Get, [this](const QString &id, const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        AppInstance *a = chains_->app(id);
        if (!a)
            return problem(Status::NotFound, "Instance not found", id);
        const int tail = qBound(1, req.query().queryItemValue("tail").toInt(), 5000);
        const int n = req.query().hasQueryItem("tail") ? tail : 100;
        return QHttpServerResponse(QJsonObject{{"lines", QJsonArray::fromStringList(a->tailLog(n))}});
    });

    s.route(P + "/operations/<arg>", Method::Get, [this](const QString &id, const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        if (!ops_->has(id))
            return problem(Status::NotFound, "Operation not found", id);
        return QHttpServerResponse(ops_->get(id));
    });

    // --- změny -------------------------------------------------------------
    s.route(P + "/validate", Method::Post, [this](const QHttpServerRequest &req) {
        REQUIRE(Role::Read);
        QJsonObject body; QString err;
        if (!parseObject(req, &body, &err))
            return problem(Status::BadRequest, "Invalid JSON", err);
        ChainSpec spec = ChainSpec::fromJson(body);
        return QHttpServerResponse(chains_->validate(spec, spec.id).toJson());
    });

    s.route(P + "/chains", Method::Post, [this, opAccepted](const QHttpServerRequest &req) {
        REQUIRE(Role::Control);
        QJsonObject body; QString err;
        if (!parseObject(req, &body, &err))
            return problem(Status::BadRequest, "Invalid JSON", err);
        const bool start = req.query().queryItemValue("start") == "true";
        ChainManager::Validation v;
        QString chainId;
        const QString opId = chains_->create(ChainSpec::fromJson(body), start, &v, &chainId);
        if (!v.valid())
            return problem(Status::UnprocessableEntity, "Invalid chain", {}, v.errors);
        audit(req, "create " + chainId + (start ? " +start" : ""));
        if (!opId.isEmpty())
            return opAccepted(opId);
        return QHttpServerResponse(chains_->chainJson(chainId), Status::Created);
    });

    s.route(P + "/chains/<arg>", Method::Put, [this, opAccepted](const QString &id, const QHttpServerRequest &req) {
        REQUIRE(Role::Control);
        QJsonObject body; QString err;
        if (!parseObject(req, &body, &err))
            return problem(Status::BadRequest, "Invalid JSON", err);
        ChainManager::Validation v;
        const QString opId = chains_->update(id, ChainSpec::fromJson(body), &v, &err);
        if (opId.isEmpty())
            return err == "invalid" ? problem(Status::UnprocessableEntity, "Invalid chain", {}, v.errors)
                                    : fromError(err);
        audit(req, "update " + id);
        return opAccepted(opId);
    });

    s.route(P + "/chains/<arg>", Method::Delete, [this, opAccepted](const QString &id, const QHttpServerRequest &req) {
        REQUIRE(Role::Control);
        QString err;
        const QString opId = chains_->remove(id, &err);
        if (opId.isEmpty())
            return fromError(err);
        audit(req, "remove " + id);
        return opAccepted(opId);
    });

    s.route(P + "/chains/<arg>/<arg>", Method::Post,
            [this, opAccepted](const QString &id, const QString &action, const QHttpServerRequest &req) {
        REQUIRE(Role::Control);
        QString err;
        const QString opId = chains_->chainAction(id, action, &err);
        if (opId.isEmpty())
            return fromError(err);
        audit(req, action + " chain " + id);
        return opAccepted(opId);
    });

    s.route(P + "/apps/<arg>/<arg>", Method::Post,
            [this, opAccepted](const QString &id, const QString &action, const QHttpServerRequest &req) {
        REQUIRE(Role::Control);
        QString err;
        const QString opId = chains_->appAction(id, action, &err);
        if (opId.isEmpty())
            return fromError(err);
        audit(req, action + " app " + id);
        return opAccepted(opId);
    });

    s.route(P + "/apps/<arg>/config", Method::Patch, [this, opAccepted](const QString &id, const QHttpServerRequest &req) {
        REQUIRE(Role::Control);
        QJsonObject body; QString err;
        if (!parseObject(req, &body, &err))
            return problem(Status::BadRequest, "Invalid JSON", err);
        const QString opId = chains_->appConfig(id, body, &err);
        if (opId.isEmpty())
            return fromError(err);
        audit(req, "config " + id + " " + QString::fromUtf8(QJsonDocument(body).toJson(QJsonDocument::Compact)));
        return opAccepted(opId);
    });
}

bool ApiServer::listen(QString *error)
{
    const QHostAddress addr(cfg_.listenAddress);
#if QT_VERSION >= QT_VERSION_CHECK(6, 8, 0)
    auto *tcp = new QTcpServer(this);
    if (!tcp->listen(addr, quint16(cfg_.listenPort)) || !server_.bind(tcp)) {
#else
    if (server_.listen(addr, quint16(cfg_.listenPort)) == 0) {
#endif
        *error = QStringLiteral("nelze naslouchat na %1:%2").arg(cfg_.listenAddress).arg(cfg_.listenPort);
        return false;
    }
    return true;
}
