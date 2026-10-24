#include "appinstance.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QProcessEnvironment>
#include <QSaveFile>
#include <memory>

AppInstance::AppInstance(LaunchSpec launch, QNetworkAccessManager *nam, QObject *parent)
    : QObject(parent), l_(std::move(launch)), nam_(nam)
{
    connect(&proc_, &QProcess::finished, this, [this](int exitCode, QProcess::ExitStatus status) {
        ready_ = false;
        hasExitCode_ = true;
        lastExitCode_ = (status == QProcess::NormalExit) ? exitCode : -1;
        if (stopRequested_) {
            setState(InstanceState::Stopped);
        } else {
            qWarning("agent: %s exited unexpectedly (code %d)", qPrintable(l_.localId), lastExitCode_);
            setState(InstanceState::Error);
            emit exitedUnexpectedly(lastExitCode_);
        }
    });
    connect(&proc_, &QProcess::errorOccurred, this, [this](QProcess::ProcessError e) {
        if (e == QProcess::FailedToStart) {
            ready_ = false;
            setState(InstanceState::Error);
        }
    });
}

AppInstance::~AppInstance()
{
    // Pozor: s QProcess zaniká aplikace spolu s agentem. Viz README – varianta se systemd.
    if (isProcessRunning()) {
        stopRequested_ = true;
        proc_.terminate();
        if (!proc_.waitForFinished(3000))
            proc_.kill();
        proc_.waitForFinished(1000);
    }
}

void AppInstance::setState(InstanceState s)
{
    if (s == state_)
        return;
    state_ = s;
    emit stateChanged(s);
}

bool AppInstance::writeConfigFile(QString *error)
{
    QSaveFile f(l_.configFile);
    if (!f.open(QIODevice::WriteOnly)) {
        if (error) *error = "nelze zapsat " + l_.configFile;
        return false;
    }
    f.write(QJsonDocument(l_.spec.config).toJson());
    return f.commit();
}

bool AppInstance::start(QString *error)
{
    if (isProcessRunning())
        return true;

    QDir().mkpath(l_.dataDir);
    QDir().mkpath(QFileInfo(l_.logFile).absolutePath());
    QDir().mkpath(QFileInfo(l_.configFile).absolutePath());
    if (!writeConfigFile(error))
        return false;

    // Spouštěcí kontrakt (čte ho libchainapp, viz chainapp.h / Identity)
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.insert("CHAIN_INSTANCE_ID", l_.instanceId);
    env.insert("CHAIN_ID", l_.chainId);
    env.insert("CHAIN_POSITION", QString::number(l_.position));
    env.insert("CHAIN_UPSTREAM_URL", l_.upstreamUrl);
    env.insert("CHAIN_DOWNSTREAM_URL", l_.downstreamUrl);
    env.insert("APP_BIND_ADDRESS", "127.0.0.1");
    env.insert("APP_PORT", QString::number(l_.spec.port));
    env.insert("APP_API_TOKEN", l_.apiToken);
    env.insert("APP_CONFIG_FILE", l_.configFile);
    env.insert("APP_DATA_DIR", l_.dataDir);

    proc_.setProcessEnvironment(env);
    proc_.setWorkingDirectory(l_.dataDir);
    proc_.setStandardOutputFile(l_.logFile, QIODevice::Append);
    proc_.setStandardErrorFile(l_.logFile, QIODevice::Append);
    proc_.setProgram(l_.executable);
    proc_.setArguments(l_.args);

    stopRequested_ = false;
    ready_ = false;
    pollFailures_ = 0;
    hasExitCode_ = false;
    lastStatus_ = {};
    setState(InstanceState::Starting);
    ++starts_;

    proc_.start();
    if (!proc_.waitForStarted(5000)) {
        if (error) *error = l_.localId + ": " + proc_.errorString();
        setState(InstanceState::Error);
        return false;
    }
    return true;
}

void AppInstance::stop(int timeoutMs, Done done)
{
    if (!isProcessRunning()) {
        ready_ = false;
        setState(InstanceState::Stopped);
        done(true, "not running");
        return;
    }
    stopRequested_ = true;
    ready_ = false;
    setState(InstanceState::Stopping);

    // 1) slušně: aplikace sama udělá drain a ukončí se
    QNetworkReply *r = request("POST", "/api/v1/control/stop");
    connect(r, &QNetworkReply::finished, r, &QObject::deleteLater);

    // 2) po timeoutu SIGTERM, po dalších 5 s SIGKILL
    auto finished = std::make_shared<bool>(false);
    auto conn = std::make_shared<QMetaObject::Connection>();
    auto *timer = new QTimer(this);
    timer->setSingleShot(true);
    timer->setInterval(timeoutMs);

    *conn = connect(&proc_, &QProcess::finished, this, [finished, conn, timer, done] {
        if (*finished)
            return;
        *finished = true;
        QObject::disconnect(*conn);
        timer->stop();
        timer->deleteLater();
        done(true, "stopped");
    });
    connect(timer, &QTimer::timeout, this, [this, finished] {
        if (*finished)
            return;
        qWarning("agent: %s did not stop in time, sending SIGTERM", qPrintable(l_.localId));
        proc_.terminate();
        QTimer::singleShot(5000, this, [this, finished] {
            if (!*finished && isProcessRunning()) {
                qWarning("agent: %s killed", qPrintable(l_.localId));
                proc_.kill();
            }
        });
    });
    timer->start();
}

QNetworkReply *AppInstance::request(const QByteArray &verb, const QString &path, const QByteArray &body)
{
    QNetworkRequest req(QUrl(QStringLiteral("http://127.0.0.1:%1%2").arg(l_.spec.port).arg(path)));
    req.setRawHeader("Authorization", "Bearer " + l_.apiToken.toUtf8());
    req.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    req.setTransferTimeout(5000);
    return nam_->sendCustomRequest(req, verb, body);
}

void AppInstance::poll()
{
    if (!isProcessRunning() || state_ == InstanceState::Stopping)
        return;

    QNetworkReply *r = request("GET", "/api/v1/health/ready");
    connect(r, &QNetworkReply::finished, this, [this, r] {
        r->deleteLater();
        const int code = r->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (code == 0) {
            // spojení se nepovedlo – při startu je to normální, jinak 3× po sobě = unreachable
            ready_ = false;
            if (state_ != InstanceState::Starting && ++pollFailures_ >= 3)
                setState(InstanceState::Unreachable);
            return;
        }
        pollFailures_ = 0;
        ready_ = (code == 200);

        QNetworkReply *s = request("GET", "/api/v1/status");
        connect(s, &QNetworkReply::finished, this, [this, s] {
            s->deleteLater();
            if (s->error() != QNetworkReply::NoError)
                return;
            lastStatus_ = QJsonDocument::fromJson(s->readAll()).object();
            lastPolledAt_ = QDateTime::currentDateTimeUtc();
            if (state_ != InstanceState::Stopping && isProcessRunning())
                setState(fromAppState(lastStatus_.value("state").toString()));
        });
    });
}

void AppInstance::control(const QString &action, Done done)
{
    if (!isProcessRunning()) {
        done(false, "not running");
        return;
    }
    QNetworkReply *r = request("POST", "/api/v1/control/" + action);
    connect(r, &QNetworkReply::finished, this, [r, done] {
        r->deleteLater();
        const int code = r->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        done(code == 202, QStringLiteral("HTTP %1").arg(code));
    });
}

void AppInstance::patchConfig(const QJsonObject &patch, Done done)
{
    if (!isProcessRunning()) {
        done(false, "not running");
        return;
    }
    QNetworkReply *r = request("PATCH", "/api/v1/config", QJsonDocument(patch).toJson(QJsonDocument::Compact));
    connect(r, &QNetworkReply::finished, this, [this, r, patch, done] {
        r->deleteLater();
        const int code = r->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (code == 200) {
            // změnu zapíšeme i do spec, aby přežila restart
            for (auto it = patch.begin(); it != patch.end(); ++it)
                l_.spec.config[it.key()] = it.value();
            writeConfigFile(nullptr);
            done(true, "applied");
        } else {
            const QJsonObject p = QJsonDocument::fromJson(r->readAll()).object();
            done(false, p.value("detail").toString(QStringLiteral("HTTP %1").arg(code)));
        }
    });
}

QStringList AppInstance::tailLog(int lines) const
{
    QFile f(l_.logFile);
    if (!f.open(QIODevice::ReadOnly))
        return {};
    const qint64 chunk = 256 * 1024;
    if (f.size() > chunk)
        f.seek(f.size() - chunk);
    QStringList all = QString::fromUtf8(f.readAll()).split('\n', Qt::SkipEmptyParts);
    if (all.size() > lines)
        all = all.mid(all.size() - lines);
    return all;
}

QJsonObject AppInstance::toJson() const
{
    return {
        {"instanceId", l_.localId},
        {"globalId", l_.instanceId},
        {"type", l_.spec.type},
        {"version", l_.spec.version},
        {"port", l_.spec.port},
        {"position", l_.position},
        {"state", toString(state_)},
        {"ready", ready_},
        {"pid", isProcessRunning() ? QJsonValue(qint64(proc_.processId())) : QJsonValue()},
        {"restarts", restarts()},
        {"lastExitCode", hasExitCode_ ? QJsonValue(lastExitCode_) : QJsonValue()},
        {"lastStatus", lastStatus_},
        {"lastPolledAt", lastPolledAt_.isValid() ? QJsonValue(lastPolledAt_.toString(Qt::ISODateWithMs)) : QJsonValue()},
    };
}
