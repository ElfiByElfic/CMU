#include "chainmanager.h"

#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonDocument>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSet>
#include <QUuid>
#include <algorithm>

QJsonObject ChainManager::Validation::toJson() const
{
    QJsonArray p;
    for (int x : ports) p.append(x);
    return {{"valid", valid()}, {"errors", QJsonArray::fromStringList(errors)},
            {"warnings", QJsonArray::fromStringList(warnings)}, {"assignedPorts", p}};
}

ChainManager::ChainManager(const AgentConfig &cfg, Catalog *catalog, Operations *ops, QObject *parent)
    : QObject(parent), cfg_(cfg), catalog_(catalog), ops_(ops)
{
    connect(&pollTimer_, &QTimer::timeout, this, [this] {
        for (const auto &rt : std::as_const(chains_))
            for (const auto &a : rt.apps)
                if (a) a->poll();
    });
    pollTimer_.start(cfg_.pollIntervalMs);
}

ChainManager::~ChainManager()
{
    for (auto &rt : chains_)
        destroyInstances(rt);
}

// ---------------------------------------------------------------- persistence

void ChainManager::save() const
{
    QDir().mkpath(cfg_.stateDir);
    QJsonArray arr;
    for (const auto &rt : chains_)
        arr.append(QJsonObject{{"spec", rt.spec.toJson()}, {"desiredRunning", rt.desiredRunning}});
    QSaveFile f(cfg_.stateDir + "/chains.json");
    if (f.open(QIODevice::WriteOnly)) {
        f.write(QJsonDocument(arr).toJson());
        f.commit();
    }
}

void ChainManager::load()
{
    QFile f(cfg_.stateDir + "/chains.json");
    if (!f.open(QIODevice::ReadOnly))
        return;
    for (const auto &v : QJsonDocument::fromJson(f.readAll()).array()) {
        const QJsonObject o = v.toObject();
        Runtime rt;
        rt.spec = ChainSpec::fromJson(o.value("spec").toObject());
        rt.desiredRunning = o.value("desiredRunning").toBool();
        Validation val = validate(rt.spec, rt.spec.id);
        if (!val.valid()) {
            qWarning("agent: chain %s invalid after load: %s", qPrintable(rt.spec.id),
                     qPrintable(val.errors.join("; ")));
        }
        chains_.insert(rt.spec.id, rt);
        buildInstances(chains_[rt.spec.id]);
    }
}

void ChainManager::autostart()
{
    for (auto it = chains_.begin(); it != chains_.end(); ++it) {
        if (it->desiredRunning && it->spec.autostart) {
            QString err;
            chainAction(it.key(), "start", &err);
        }
    }
}

// ---------------------------------------------------------------- validace

ChainManager::Validation ChainManager::validate(ChainSpec &spec, const QString &replacingId) const
{
    Validation v;
    static const QRegularExpression idRe("^[a-z0-9-]{1,40}$");

    if (spec.id.isEmpty()) {
        do {
            spec.id = "chain-" + QUuid::createUuid().toString(QUuid::WithoutBraces).left(6);
        } while (chains_.contains(spec.id));
    } else if (!idRe.match(spec.id).hasMatch()) {
        v.errors << "id smí obsahovat jen a-z, 0-9 a '-' (max 40 znaků)";
    } else if (spec.id != replacingId && chains_.contains(spec.id)) {
        v.errors << "řetězec s id " + spec.id + " už existuje";
    }

    spec.name = spec.name.trimmed();
    if (spec.name.isEmpty())
        v.errors << "chybí název řetězce";
    for (auto it = chains_.cbegin(); it != chains_.cend(); ++it)
        if (it.key() != replacingId && it->spec.name.compare(spec.name, Qt::CaseInsensitive) == 0)
            v.errors << "název '" + spec.name + "' už na VM používá řetězec " + it.key();

    if (spec.apps.size() < 2)
        v.errors << "řetězec potřebuje alespoň 2 aplikace";
    if (!QStringList{"never", "on-failure", "always"}.contains(spec.restartPolicy))
        v.errors << "restartPolicy musí být never | on-failure | always";

    // Obsazené porty ostatních řetězců
    QSet<int> used;
    for (auto it = chains_.cbegin(); it != chains_.cend(); ++it)
        if (it.key() != replacingId)
            for (const auto &a : it->spec.apps)
                used.insert(a.port);

    QSet<QString> types;
    for (auto &a : spec.apps) {
        if (types.contains(a.type))
            v.errors << a.type + " je v řetězci dvakrát";
        types.insert(a.type);
        if (!catalog_->has(a.type, a.version))
            v.errors << a.type + " " + a.version + " není v katalogu VM";
        if (cfg_.environment == "production" && a.version.contains('-'))
            v.warnings << a.type + " " + a.version + " je předběžná verze na produkční VM";
        if (a.port > 0) {
            if (a.port < cfg_.portMin || a.port > cfg_.portMax)
                v.errors << QStringLiteral("port %1 mimo rozsah %2–%3").arg(a.port).arg(cfg_.portMin).arg(cfg_.portMax);
            else if (used.contains(a.port))
                v.errors << QStringLiteral("port %1 je obsazený").arg(a.port);
            used.insert(a.port);
        }
    }
    for (auto &a : spec.apps) {
        if (a.port == 0) {
            int p = cfg_.portMin;
            while (p <= cfg_.portMax && used.contains(p)) ++p;
            if (p > cfg_.portMax) { v.errors << "došly volné porty"; break; }
            a.port = p;
            used.insert(p);
        }
        v.ports << a.port;
    }
    return v;
}

// ---------------------------------------------------------------- instance

void ChainManager::buildInstances(Runtime &rt)
{
    const QString chainId = rt.spec.id;
    for (int i = 0; i < rt.spec.apps.size(); ++i) {
        const AppSpec &a = rt.spec.apps[i];
        const CatalogEntry e = catalog_->get(a.type, a.version);
        const QString slug = a.type.toLower();

        LaunchSpec l;
        l.localId = chainId + "." + slug;
        l.instanceId = cfg_.vmName + "/" + chainId + "/" + slug;
        l.chainId = chainId;
        l.position = i;
        l.spec = a;
        l.executable = e.executable;
        l.args = e.args;
        if (i > 0)
            l.upstreamUrl = QStringLiteral("http://127.0.0.1:%1").arg(rt.spec.apps[i - 1].port);
        if (i + 1 < rt.spec.apps.size())
            l.downstreamUrl = QStringLiteral("http://127.0.0.1:%1").arg(rt.spec.apps[i + 1].port);
        l.dataDir = cfg_.stateDir + "/instances/" + l.localId;
        l.configFile = l.dataDir + "/config.json";
        l.logFile = cfg_.logDir + "/" + l.localId + ".log";
        l.apiToken = QUuid::createUuid().toString(QUuid::WithoutBraces);

        auto *inst = new AppInstance(l, &nam_, this);
        connect(inst, &AppInstance::exitedUnexpectedly, this, [this, chainId, inst](int) {
            onUnexpectedExit(chainId, inst);
        });
        rt.apps.append(inst);
    }
}

void ChainManager::destroyInstances(Runtime &rt)
{
    for (auto &a : rt.apps)
        delete a.data();
    rt.apps.clear();
}

void ChainManager::onUnexpectedExit(const QString &chainId, AppInstance *a)
{
    if (!chains_.contains(chainId))
        return;
    const Runtime &rt = chains_[chainId];
    if (!rt.desiredRunning || rt.spec.restartPolicy == "never" || !rt.busyOp.isEmpty())
        return;
    // exponenciální backoff 2, 4, 8 … max 60 s
    const int delay = std::min(60, 1 << std::min(a->restarts() + 1, 6));
    qWarning("agent: restarting %s in %d s", qPrintable(a->id()), delay);
    QPointer<AppInstance> pa(a);
    QTimer::singleShot(delay * 1000, this, [this, chainId, pa] {
        if (pa && chains_.contains(chainId) && chains_[chainId].desiredRunning && !pa->isProcessRunning())
            pa->start();
    });
}

// ---------------------------------------------------------------- sekvence

void ChainManager::waitReady(AppInstance *a, std::function<void(bool, const QString &)> done)
{
    auto *t = new QTimer(this);
    t->setInterval(500);
    QElapsedTimer elapsed;
    elapsed.start();
    QPointer<AppInstance> pa(a);
    const int timeout = cfg_.readyTimeoutMs;
    connect(t, &QTimer::timeout, this, [t, pa, elapsed, timeout, done] {
        auto finish = [t, done](bool ok, const QString &msg) { t->stop(); t->deleteLater(); done(ok, msg); };
        if (!pa)                                   return finish(false, "instance removed");
        if (pa->isReady())                         return finish(true, "ready");
        if (pa->state() == InstanceState::Error)   return finish(false, "process exited during start");
        if (elapsed.elapsed() > timeout)           return finish(false, "not ready within timeout");
        pa->poll();
    });
    t->start();
}

// Spouští odzadu: order je už otočené pořadí (výstup → vstup).
void ChainManager::startNext(const QString &opId, Apps order, int i, Done done)
{
    if (i >= order.size())
        return done(true);
    AppInstance *a = order[i];
    if (!a)
        return done(false);
    if (a->isReady()) {
        ops_->step(opId, a->id(), "succeeded", "already running");
        return startNext(opId, order, i + 1, done);
    }
    ops_->step(opId, a->id(), "running", "starting");
    QString err;
    if (!a->start(&err)) {
        ops_->step(opId, a->id(), "failed", err);
        return done(false);
    }
    waitReady(a, [this, opId, order, i, done, id = a->id()](bool ok, const QString &msg) {
        ops_->step(opId, id, ok ? "succeeded" : "failed", msg);
        if (!ok)
            return done(false);
        startNext(opId, order, i + 1, done);
    });
}

// Zastavuje odpředu (vstup → výstup), každou instanci přes drain.
void ChainManager::stopNext(const QString &opId, Apps order, int i, Done done)
{
    if (i >= order.size())
        return done(true);
    AppInstance *a = order[i];
    if (!a)
        return stopNext(opId, order, i + 1, done);
    ops_->step(opId, a->id(), "running", "stopping");
    a->stop(cfg_.stopTimeoutMs, [this, opId, order, i, done, id = a->id()](bool ok, const QString &msg) {
        ops_->step(opId, id, ok ? "succeeded" : "failed", msg);
        stopNext(opId, order, i + 1, done);
    });
}

void ChainManager::startChainSeq(const QString &chainId, const QString &opId, Done done)
{
    Apps order = chains_[chainId].apps;
    std::reverse(order.begin(), order.end());
    startNext(opId, order, 0, done);
}

void ChainManager::stopChainSeq(const QString &chainId, const QString &opId, Done done)
{
    stopNext(opId, chains_[chainId].apps, 0, done);
}

QString ChainManager::beginChainOp(const QString &chainId, const QString &kind, QString *error)
{
    if (!chains_.contains(chainId)) {
        if (error) *error = "not-found";
        return {};
    }
    Runtime &rt = chains_[chainId];
    if (!rt.busyOp.isEmpty()) {
        if (error) *error = "busy:" + rt.busyOp;
        return {};
    }
    rt.busyOp = ops_->begin(kind, chainId);
    return rt.busyOp;
}

void ChainManager::endChainOp(const QString &chainId, const QString &opId, bool ok, const QString &msg)
{
    if (chains_.contains(chainId))
        chains_[chainId].busyOp.clear();
    ops_->finish(opId, ok, msg);
}

// ---------------------------------------------------------------- akce

QString ChainManager::create(ChainSpec spec, bool start, Validation *v, QString *chainId)
{
    *v = validate(spec);
    if (!v->valid())
        return {};
    Runtime rt;
    rt.spec = spec;
    rt.desiredRunning = start;
    chains_.insert(spec.id, rt);
    buildInstances(chains_[spec.id]);
    save();
    if (chainId) *chainId = spec.id;
    if (!start)
        return {};
    QString err;
    return chainAction(spec.id, "start", &err);
}

QString ChainManager::update(const QString &chainId, ChainSpec spec, Validation *v, QString *error)
{
    if (!chains_.contains(chainId)) {
        if (error) *error = "not-found";
        return {};
    }
    spec.id = chainId;
    *v = validate(spec, chainId);
    if (!v->valid()) {
        if (error) *error = "invalid";
        return {};
    }
    const QString opId = beginChainOp(chainId, "chain.update", error);
    if (opId.isEmpty())
        return {};
    stopChainSeq(chainId, opId, [this, chainId, opId, spec](bool) {
        Runtime &rt = chains_[chainId];
        destroyInstances(rt);
        rt.spec = spec;
        buildInstances(rt);
        save();
        if (!rt.desiredRunning)
            return endChainOp(chainId, opId, true, "updated");
        startChainSeq(chainId, opId, [this, chainId, opId](bool ok) {
            endChainOp(chainId, opId, ok, ok ? "updated and started" : "updated, start failed");
        });
    });
    return opId;
}

QString ChainManager::remove(const QString &chainId, QString *error)
{
    const QString opId = beginChainOp(chainId, "chain.remove", error);
    if (opId.isEmpty())
        return {};
    chains_[chainId].desiredRunning = false;
    stopChainSeq(chainId, opId, [this, chainId, opId](bool) {
        destroyInstances(chains_[chainId]);
        chains_.remove(chainId);
        save();
        ops_->finish(opId, true, "removed");
    });
    return opId;
}

QString ChainManager::chainAction(const QString &chainId, const QString &action, QString *error)
{
    if (!QStringList{"start", "stop", "restart"}.contains(action)) {
        if (error) *error = "unknown-action";
        return {};
    }
    const QString opId = beginChainOp(chainId, "chain." + action, error);
    if (opId.isEmpty())
        return {};
    Runtime &rt = chains_[chainId];
    rt.desiredRunning = (action != "stop");
    save();

    auto doStart = [this, chainId, opId] {
        startChainSeq(chainId, opId, [this, chainId, opId](bool ok) {
            endChainOp(chainId, opId, ok, ok ? "chain running" : "chain start failed");
        });
    };
    if (action == "start") {
        doStart();
    } else if (action == "stop") {
        stopChainSeq(chainId, opId, [this, chainId, opId](bool ok) {
            endChainOp(chainId, opId, ok, "chain stopped");
        });
    } else {
        stopChainSeq(chainId, opId, [doStart](bool) { doStart(); });
    }
    return opId;
}

QString ChainManager::chainOf(const QString &instanceId) const
{
    for (auto it = chains_.cbegin(); it != chains_.cend(); ++it)
        for (const auto &a : it->apps)
            if (a && a->id() == instanceId)
                return it.key();
    return {};
}

AppInstance *ChainManager::app(const QString &instanceId) const
{
    const QString c = chainOf(instanceId);
    if (c.isEmpty())
        return nullptr;
    for (const auto &a : chains_[c].apps)
        if (a && a->id() == instanceId)
            return a;
    return nullptr;
}

QString ChainManager::appAction(const QString &instanceId, const QString &action, QString *error)
{
    AppInstance *a = app(instanceId);
    if (!a) {
        if (error) *error = "not-found";
        return {};
    }
    const QString opId = ops_->begin("app." + action, instanceId);
    QPointer<AppInstance> pa(a);
    auto finish = [this, opId](bool ok, const QString &msg) { ops_->finish(opId, ok, msg); };

    auto doStart = [this, pa, finish] {
        if (!pa) return finish(false, "instance removed");
        QString err;
        if (!pa->start(&err)) return finish(false, err);
        waitReady(pa, finish);
    };

    if (action == "start") {
        doStart();
    } else if (action == "stop") {
        a->stop(cfg_.stopTimeoutMs, finish);
    } else if (action == "restart") {
        a->stop(cfg_.stopTimeoutMs, [doStart](bool, const QString &) { doStart(); });
    } else if (QStringList{"drain", "pause", "resume", "reload"}.contains(action)) {
        a->control(action, finish);
    } else {
        ops_->finish(opId, false, "unknown action");
        if (error) *error = "unknown-action";
        return {};
    }
    return opId;
}

QString ChainManager::appConfig(const QString &instanceId, const QJsonObject &patch, QString *error)
{
    AppInstance *a = app(instanceId);
    if (!a) {
        if (error) *error = "not-found";
        return {};
    }
    const QString opId = ops_->begin("app.config", instanceId);
    const QString chainId = chainOf(instanceId);
    const int pos = a->launch().position;
    a->patchConfig(patch, [this, opId, chainId, pos, patch](bool ok, const QString &msg) {
        if (ok && chains_.contains(chainId)) {
            // zapsat i do definice řetězce, aby změna přežila restart agenta
            QJsonObject &cfg = chains_[chainId].spec.apps[pos].config;
            for (auto it = patch.begin(); it != patch.end(); ++it)
                cfg[it.key()] = it.value();
            save();
        }
        ops_->finish(opId, ok, msg);
    });
    return opId;
}

// ---------------------------------------------------------------- stav

QString ChainManager::chainState(const Runtime &rt) const
{
    int stopped = 0, starting = 0, stopping = 0, broken = 0, degraded = 0;
    for (const auto &a : rt.apps) {
        if (!a) continue;
        switch (a->state()) {
        case InstanceState::Stopped:     ++stopped; break;
        case InstanceState::Starting:    ++starting; break;
        case InstanceState::Stopping:
        case InstanceState::Draining:    ++stopping; break;
        case InstanceState::Error:
        case InstanceState::Unreachable: ++broken; break;
        case InstanceState::Degraded:
        case InstanceState::Paused:      ++degraded; break;
        case InstanceState::Running:     break;
        }
    }
    if (stopped == rt.apps.size()) return "stopped";
    if (starting)                  return "starting";
    if (stopping)                  return "stopping";
    if (broken || stopped)         return "broken";
    if (degraded)                  return "degraded";
    return "running";
}

QJsonObject ChainManager::chainJson(const QString &chainId) const
{
    const Runtime &rt = chains_[chainId];
    QJsonArray apps;
    for (const auto &a : rt.apps)
        if (a) apps.append(a->toJson());
    return {{"spec", rt.spec.toJson()}, {"state", chainState(rt)}, {"desiredRunning", rt.desiredRunning},
            {"busyOperation", rt.busyOp.isEmpty() ? QJsonValue() : QJsonValue(rt.busyOp)}, {"apps", apps}};
}

QJsonArray ChainManager::chainsJson() const
{
    QJsonArray arr;
    for (auto it = chains_.cbegin(); it != chains_.cend(); ++it)
        arr.append(chainJson(it.key()));
    return arr;
}

QString ChainManager::vmState() const
{
    bool warn = false;
    for (const auto &rt : chains_) {
        const QString s = chainState(rt);
        // zastavený řetězec, který zastavený být má, není problém
        if (s == "broken" || (s == "stopped" && rt.desiredRunning))
            return "problem";
        if (s == "degraded" || s == "starting" || s == "stopping")
            warn = true;
    }
    return warn ? "warning" : "ok";
}
