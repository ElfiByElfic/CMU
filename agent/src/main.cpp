// chain-agent – správce řetězců aplikací na jedné VM.
// Použití: chain-agent --config /etc/chain-agent/agent.json

#include "apiserver.h"
#include "catalog.h"
#include "chainmanager.h"
#include "operations.h"

#include <QCommandLineParser>
#include <QCoreApplication>
#include <QFile>
#include <QJsonDocument>

int main(int argc, char *argv[])
{
    QCoreApplication app(argc, argv);
    QCoreApplication::setApplicationName("chain-agent");
    QCoreApplication::setApplicationVersion("1.0.0");

    QCommandLineParser parser;
    parser.addHelpOption();
    parser.addVersionOption();
    QCommandLineOption configOpt("config", "Konfigurační soubor agenta.", "file", "/etc/chain-agent/agent.json");
    parser.addOption(configOpt);
    parser.process(app);

    QFile f(parser.value(configOpt));
    if (!f.open(QIODevice::ReadOnly)) {
        qCritical("chain-agent: nelze otevřít %s", qPrintable(f.fileName()));
        return 1;
    }
    const AgentConfig cfg = AgentConfig::fromJson(QJsonDocument::fromJson(f.readAll()).object());
    if (cfg.vmName.isEmpty() || cfg.controlTokens.isEmpty()) {
        qCritical("chain-agent: v konfiguraci chybí vmName nebo controlTokens");
        return 1;
    }

    Catalog catalog(cfg.catalogDir);
    catalog.rescan();

    Operations ops;
    ChainManager chains(cfg, &catalog, &ops);
    chains.load();

    ApiServer api(cfg, &chains, &catalog, &ops);
    QString err;
    if (!api.listen(&err)) {
        qCritical("chain-agent: %s", qPrintable(err));
        return 1;
    }
    qInfo("chain-agent %s: VM %s, API na %s:%d",
          qPrintable(QCoreApplication::applicationVersion()), qPrintable(cfg.vmName),
          qPrintable(cfg.listenAddress), cfg.listenPort);

    chains.autostart();
    return app.exec();
}
