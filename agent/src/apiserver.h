#pragma once
// REST API agenta – implementace api/agent-api.yaml.

#include "catalog.h"
#include "chainmanager.h"
#include "model.h"
#include "operations.h"

#include <QDateTime>
#include <QHttpServer>
#include <QObject>

class ApiServer : public QObject
{
    Q_OBJECT
public:
    ApiServer(const AgentConfig &cfg, ChainManager *chains, Catalog *catalog, Operations *ops,
              QObject *parent = nullptr);
    bool listen(QString *error);

private:
    enum class Role { None, Read, Control };
    Role role(const QHttpServerRequest &req) const;
    void audit(const QHttpServerRequest &req, const QString &what) const;
    QJsonObject info() const;
    void setupRoutes();

    AgentConfig cfg_;
    ChainManager *chains_;
    Catalog *catalog_;
    Operations *ops_;
    QHttpServer server_;
    QDateTime startedAt_ = QDateTime::currentDateTimeUtc();
};
