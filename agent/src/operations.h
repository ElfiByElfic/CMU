#pragma once
// Registr asynchronních operací (schéma Operation v agent-api.yaml).

#include <QDateTime>
#include <QJsonArray>
#include <QJsonObject>
#include <QMap>
#include <QUuid>

class Operations
{
public:
    QString begin(const QString &kind, const QString &target)
    {
        const QString id = QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
        ops_[id] = QJsonObject{{"id", id}, {"kind", kind}, {"target", target}, {"status", "running"},
                               {"message", ""}, {"steps", QJsonArray()},
                               {"startedAt", now()}, {"finishedAt", QJsonValue()}};
        order_.append(id);
        while (order_.size() > 200)
            ops_.remove(order_.takeFirst());
        return id;
    }

    // Vloží nebo aktualizuje krok podle target.
    void step(const QString &id, const QString &target, const QString &status, const QString &message = {})
    {
        if (!ops_.contains(id))
            return;
        QJsonObject op = ops_[id];
        QJsonArray steps = op["steps"].toArray();
        const QJsonObject s{{"target", target}, {"status", status}, {"message", message}};
        bool found = false;
        for (int i = 0; i < steps.size(); ++i) {
            if (steps[i].toObject().value("target").toString() == target) {
                steps[i] = s;
                found = true;
            }
        }
        if (!found)
            steps.append(s);
        op["steps"] = steps;
        ops_[id] = op;
    }

    void finish(const QString &id, bool ok, const QString &message)
    {
        if (!ops_.contains(id))
            return;
        QJsonObject op = ops_[id];
        op["status"] = ok ? "succeeded" : "failed";
        op["message"] = message;
        op["finishedAt"] = now();
        ops_[id] = op;
    }

    bool has(const QString &id) const { return ops_.contains(id); }
    QJsonObject get(const QString &id) const { return ops_.value(id); }

private:
    static QString now() { return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs); }
    QMap<QString, QJsonObject> ops_;
    QStringList order_;
};
