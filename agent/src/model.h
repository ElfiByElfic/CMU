#pragma once
// Datový model agenta – odpovídá schématům v api/agent-api.yaml.

#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QString>
#include <QStringList>

// Stav instance pohledem agenta (InstanceState).
enum class InstanceState { Stopped, Starting, Running, Degraded, Paused, Draining, Stopping, Error, Unreachable };

inline QString toString(InstanceState s)
{
    switch (s) {
    case InstanceState::Stopped:     return "stopped";
    case InstanceState::Starting:    return "starting";
    case InstanceState::Running:     return "running";
    case InstanceState::Degraded:    return "degraded";
    case InstanceState::Paused:      return "paused";
    case InstanceState::Draining:    return "draining";
    case InstanceState::Stopping:    return "stopping";
    case InstanceState::Error:       return "error";
    case InstanceState::Unreachable: return "unreachable";
    }
    return "error";
}

// Převod stavu hlášeného aplikací (AppState z app-api.yaml).
inline InstanceState fromAppState(const QString &s)
{
    if (s == "starting") return InstanceState::Starting;
    if (s == "running")  return InstanceState::Running;
    if (s == "degraded") return InstanceState::Degraded;
    if (s == "paused")   return InstanceState::Paused;
    if (s == "draining") return InstanceState::Draining;
    if (s == "stopping") return InstanceState::Stopping;
    return InstanceState::Error;
}

struct AppSpec {
    QString type;
    QString version;
    int port = 0;          // 0 = přidělí agent
    QJsonObject config;

    QJsonObject toJson() const
    {
        QJsonObject o{{"type", type}, {"version", version}, {"config", config}};
        o["port"] = port > 0 ? QJsonValue(port) : QJsonValue();
        return o;
    }
    static AppSpec fromJson(const QJsonObject &o)
    {
        AppSpec a;
        a.type = o.value("type").toString();
        a.version = o.value("version").toString();
        a.port = o.value("port").toInt(0);
        a.config = o.value("config").toObject();
        return a;
    }
};

struct ChainSpec {
    QString id;
    QString name;
    QList<AppSpec> apps;
    QString restartPolicy = "on-failure";   // never | on-failure | always
    bool autostart = true;

    QJsonObject toJson() const
    {
        QJsonArray arr;
        for (const auto &a : apps)
            arr.append(a.toJson());
        return {{"id", id}, {"name", name}, {"apps", arr},
                {"restartPolicy", restartPolicy}, {"autostart", autostart}};
    }
    static ChainSpec fromJson(const QJsonObject &o)
    {
        ChainSpec c;
        c.id = o.value("id").toString();
        c.name = o.value("name").toString();
        for (const auto &v : o.value("apps").toArray())
            c.apps.append(AppSpec::fromJson(v.toObject()));
        c.restartPolicy = o.value("restartPolicy").toString("on-failure");
        c.autostart = o.value("autostart").toBool(true);
        return c;
    }
};

struct AgentConfig {
    QString vmName;
    QString environment = "production";
    QString listenAddress = "0.0.0.0";
    int listenPort = 9100;
    QStringList readTokens;
    QStringList controlTokens;
    QString catalogDir = "/opt/chain-apps";
    QString stateDir = "/var/lib/chain-agent";
    QString logDir = "/var/log/chain-agent";
    int portMin = 8500;
    int portMax = 8999;
    int pollIntervalMs = 3000;
    int readyTimeoutMs = 60000;
    int stopTimeoutMs = 30000;

    static AgentConfig fromJson(const QJsonObject &o)
    {
        AgentConfig c;
        auto strList = [](const QJsonValue &v) {
            QStringList l;
            for (const auto &x : v.toArray()) l << x.toString();
            return l;
        };
        c.vmName = o.value("vmName").toString();
        c.environment = o.value("environment").toString(c.environment);
        c.listenAddress = o.value("listenAddress").toString(c.listenAddress);
        c.listenPort = o.value("listenPort").toInt(c.listenPort);
        c.readTokens = strList(o.value("readTokens"));
        c.controlTokens = strList(o.value("controlTokens"));
        c.catalogDir = o.value("catalogDir").toString(c.catalogDir);
        c.stateDir = o.value("stateDir").toString(c.stateDir);
        c.logDir = o.value("logDir").toString(c.logDir);
        const QJsonArray range = o.value("portRange").toArray();
        if (range.size() == 2) { c.portMin = range[0].toInt(); c.portMax = range[1].toInt(); }
        c.pollIntervalMs = o.value("pollIntervalMs").toInt(c.pollIntervalMs);
        c.readyTimeoutMs = o.value("readyTimeoutMs").toInt(c.readyTimeoutMs);
        c.stopTimeoutMs = o.value("stopTimeoutMs").toInt(c.stopTimeoutMs);
        return c;
    }
};
