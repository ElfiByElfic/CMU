#pragma once
// Správa řetězců: požadovaný stav (chains.json), validace, porty,
// spouštění odzadu / zastavování odpředu, restart po pádu.

#include "appinstance.h"
#include "catalog.h"
#include "model.h"
#include "operations.h"

#include <QMap>
#include <QNetworkAccessManager>
#include <QObject>
#include <QPointer>
#include <QTimer>
#include <functional>

class ChainManager : public QObject
{
    Q_OBJECT
public:
    struct Validation {
        QStringList errors;
        QStringList warnings;
        QList<int> ports;
        bool valid() const { return errors.isEmpty(); }
        QJsonObject toJson() const;
    };

    ChainManager(const AgentConfig &cfg, Catalog *catalog, Operations *ops, QObject *parent = nullptr);
    ~ChainManager() override;

    void load();        // načte chains.json a připraví instance
    void autostart();   // spustí řetězce, které mají běžet

    // Doplní id a porty. replacingId = id řetězce, který se upravuje.
    Validation validate(ChainSpec &spec, const QString &replacingId = {}) const;

    // Všechny akce vrací ID operace; prázdný řetězec = odmítnuto (důvod v *error).
    QString create(ChainSpec spec, bool start, Validation *v, QString *chainId);
    QString update(const QString &chainId, ChainSpec spec, Validation *v, QString *error);
    QString remove(const QString &chainId, QString *error);
    QString chainAction(const QString &chainId, const QString &action, QString *error);
    QString appAction(const QString &instanceId, const QString &action, QString *error);
    QString appConfig(const QString &instanceId, const QJsonObject &patch, QString *error);

    bool hasChain(const QString &chainId) const { return chains_.contains(chainId); }
    AppInstance *app(const QString &instanceId) const;

    QJsonObject chainJson(const QString &chainId) const;
    QJsonArray chainsJson() const;
    QString vmState() const;   // ok | warning | problem

private:
    struct Runtime {
        ChainSpec spec;
        bool desiredRunning = false;
        QList<QPointer<AppInstance>> apps;   // v pořadí toku dat
        QString busyOp;                      // probíhající operace nad řetězcem
    };
    using Done = std::function<void(bool ok)>;
    using Apps = QList<QPointer<AppInstance>>;

    void buildInstances(Runtime &rt);
    void destroyInstances(Runtime &rt);
    void startNext(const QString &opId, Apps order, int i, Done done);
    void stopNext(const QString &opId, Apps order, int i, Done done);
    void waitReady(AppInstance *a, std::function<void(bool, const QString &)> done);
    void startChainSeq(const QString &chainId, const QString &opId, Done done);
    void stopChainSeq(const QString &chainId, const QString &opId, Done done);
    QString beginChainOp(const QString &chainId, const QString &kind, QString *error);
    void endChainOp(const QString &chainId, const QString &opId, bool ok, const QString &msg);
    QString chainState(const Runtime &rt) const;
    QString chainOf(const QString &instanceId) const;
    void onUnexpectedExit(const QString &chainId, AppInstance *a);
    void save() const;

    AgentConfig cfg_;
    Catalog *catalog_;
    Operations *ops_;
    QNetworkAccessManager nam_;
    QTimer pollTimer_;
    QMap<QString, Runtime> chains_;
};
