// Příklad: jak přizpůsobit existující aplikaci pro řetězec.
// Celá integrace = kroky 1–6 níže; vlastní logika aplikace zůstává beze změny.

#include <chainapp/chainapp.h>

#include <QCoreApplication>
#include <QHttpServer>
#include <QHttpServerRequest>
#include <QHttpServerResponse>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QTimer>

using chainapp::ChainApp;
using chainapp::CheckResult;
using chainapp::State;

int main(int argc, char *argv[])
{
    QCoreApplication app(argc, argv);

    // 1) Identita aplikace (typ, verze, build – verzi a commit dodá CI)
    ChainApp chain("App2", "2.4.1", "a1f3c9e");

    // 2) Konfigurace: výchozí hodnoty, co jde měnit za běhu, validace
    chain.addConfig("timeoutMs", 5000, true, false, [](const QJsonValue &v) {
        return v.toInt() >= 100 && v.toInt() <= 60000 ? QString() : QStringLiteral("100–60000");
    });
    chain.addConfig("batchSize", 512, true);
    chain.addConfig("dbPassword", "", false, true);   // secret – v /config maskované

    // 3) Kontroly připravenosti – tady dostupnost downstreamu
    QNetworkAccessManager nam;
    bool downstreamOk = true;
    QString downstreamDetail;
    chain.addReadinessCheck("downstream", [&] {
        if (chain.identity().downstreamUrl.isEmpty())
            return CheckResult{true, "výstup řetězce"};
        return CheckResult{downstreamOk, downstreamDetail};
    });

    // 4) Reakce na povely agenta
    QTimer worker;
    bool accepting = true;
    QObject::connect(&chain, &ChainApp::drainRequested, [&] {
        accepting = false;                       // nepřijímat nové zprávy
        // … dokončit rozpracované, pak:
        QTimer::singleShot(500, &chain, &ChainApp::drainFinished);
    });
    QObject::connect(&chain, &ChainApp::pauseRequested, [&] { worker.stop(); });
    QObject::connect(&chain, &ChainApp::resumeRequested, [&] { accepting = true; worker.start(); });
    QObject::connect(&chain, &ChainApp::configChanged, [](const QString &key, const QJsonValue &v) {
        qInfo("config %s = %s", qPrintable(key), qPrintable(v.toVariant().toString()));
    });

    // 5) Start knihovny (načte prostředí od agenta a spustí povinné API)
    QString err;
    if (!chain.start(&err)) {
        qCritical("start failed: %s", qPrintable(err));
        return 1;
    }

    // Vlastní (nepovinný) endpoint aplikace na stejném serveru
    chain.server()->route("/api/v1/app2/ping", QHttpServerRequest::Method::Get,
                          [](const QHttpServerRequest &req) {
        return QHttpServerResponse(QJsonObject{
            {"pong", true}, {"correlationId", QString::fromUtf8(ChainApp::correlationId(req))}});
    });

    // Periodická kontrola downstreamu pro readiness
    QTimer probe;
    QObject::connect(&probe, &QTimer::timeout, [&] {
        const QString url = chain.identity().downstreamUrl;
        if (url.isEmpty()) return;
        QNetworkRequest r(QUrl(url + "/api/v1/health/live"));
        r.setTransferTimeout(2000);
        QNetworkReply *rep = nam.get(r);
        QObject::connect(rep, &QNetworkReply::finished, [&, rep] {
            downstreamOk = rep->error() == QNetworkReply::NoError;
            downstreamDetail = downstreamOk ? QString() : rep->errorString();
            chain.setState(downstreamOk ? State::Running : State::Degraded,
                           downstreamOk ? QString() : "downstream unreachable");
            rep->deleteLater();
        });
    });
    probe.start(3000);

    // „Zpracování“ – sem patří skutečná logika aplikace
    QObject::connect(&worker, &QTimer::timeout, [&] {
        if (!accepting) return;
        chain.markProcessed(chain.config("batchSize").toInt());
        chain.setQueueDepth(0);
    });
    worker.start(1000);

    // 6) Hotovo s inicializací → Running (od teď /health/ready vrací 200)
    chain.setState(State::Running);
    return app.exec();
}
