#pragma once
// Jedna běžící instance aplikace: proces + komunikace s jejím REST API.

#include "model.h"

#include <QDateTime>
#include <QJsonObject>
#include <QObject>
#include <QProcess>
#include <QTimer>
#include <functional>

class QNetworkAccessManager;
class QNetworkReply;

struct LaunchSpec {
    QString localId;        // chain-a.app2   – ID v API agenta
    QString instanceId;     // vm-prod-02/chain-a/app2 – globální ID (CHAIN_INSTANCE_ID)
    QString chainId;
    int position = 0;
    AppSpec spec;           // port už je přidělený
    QString executable;
    QStringList args;
    QString upstreamUrl;
    QString downstreamUrl;
    QString dataDir;
    QString logFile;
    QString configFile;
    QString apiToken;       // token, kterým se agent prokazuje aplikaci
};

class AppInstance : public QObject
{
    Q_OBJECT
public:
    using Done = std::function<void(bool ok, const QString &message)>;

    AppInstance(LaunchSpec launch, QNetworkAccessManager *nam, QObject *parent = nullptr);
    ~AppInstance() override;

    const LaunchSpec &launch() const { return l_; }
    QString id() const { return l_.localId; }
    InstanceState state() const { return state_; }
    bool isReady() const { return ready_; }
    bool isProcessRunning() const { return proc_.state() != QProcess::NotRunning; }
    int restarts() const { return starts_ > 0 ? starts_ - 1 : 0; }

    bool start(QString *error = nullptr);         // spustí proces (stav Starting)
    void stop(int timeoutMs, Done done);          // drain+stop přes API, pak SIGTERM, pak SIGKILL
    void poll();                                  // GET /health/ready + /status
    void control(const QString &action, Done done);   // drain|pause|resume|reload
    void patchConfig(const QJsonObject &patch, Done done);
    QStringList tailLog(int lines) const;

    QJsonObject toJson() const;

signals:
    void stateChanged(InstanceState state);
    void exitedUnexpectedly(int exitCode);

private:
    void setState(InstanceState s);
    QNetworkReply *request(const QByteArray &verb, const QString &path, const QByteArray &body = {});
    bool writeConfigFile(QString *error);

    LaunchSpec l_;
    QNetworkAccessManager *nam_;
    QProcess proc_;
    InstanceState state_ = InstanceState::Stopped;
    bool ready_ = false;
    bool stopRequested_ = false;
    int starts_ = 0;
    int lastExitCode_ = -1;
    bool hasExitCode_ = false;
    QJsonObject lastStatus_;
    QDateTime lastPolledAt_;
    int pollFailures_ = 0;
};
