package org.ohdsi.lab;

import org.ohdsi.circe.cohortdefinition.CohortExpressionQueryBuilder;
import org.ohdsi.circe.cohortdefinition.CohortExpressionQueryBuilder.BuildExpressionQueryOptions;
import org.ohdsi.sql.SqlRender;
import org.ohdsi.sql.SqlTranslate;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/**
 * Compile a native OHDSI CIRCE JSON (stdin) into executable DuckDB SQL (stdout),
 * using the exact OHDSI circe-be library that ATLAS uses + SqlRender for dialect
 * translation. The cohort is materialized into &lt;result-schema&gt;.&lt;target-table&gt;
 * (we ATTACH an in-memory schema for that at execution time so the read-only CDM is
 * never mutated), and the caller appends a final SELECT of subject_id.
 *
 *   java -cp "build:lib/*" org.ohdsi.lab.CompileCohort \
 *        --cdm-schema omop_gold --vocab-schema athena \
 *        --result-schema results --target-table cohort --target-cohort-id 1  < circe.json
 */
public final class CompileCohort {

    public static void main(String[] args) throws Exception {
        String cdmSchema = "omop_gold";
        String vocabSchema = "athena";
        String resultSchema = "results";
        String targetTable = "results.cohort";   // schema-qualified -> writable attached schema
        int cohortId = 1;
        String dialect = "duckdb";

        for (int i = 0; i + 1 < args.length; i++) {
            switch (args[i]) {
                case "--cdm-schema":      cdmSchema = args[++i]; break;
                case "--vocab-schema":    vocabSchema = args[++i]; break;
                case "--result-schema":   resultSchema = args[++i]; break;
                case "--target-table":    targetTable = args[++i]; break;
                case "--target-cohort-id": cohortId = Integer.parseInt(args[++i]); break;
                case "--dialect":         dialect = args[++i]; break;
                default: break;
            }
        }

        String circeJson = readAll(System.in);

        CohortExpressionQueryBuilder builder = new CohortExpressionQueryBuilder();
        BuildExpressionQueryOptions options = new BuildExpressionQueryOptions();
        options.cohortId = cohortId;
        options.cdmSchema = cdmSchema;
        options.vocabularySchema = vocabSchema;
        options.resultSchema = resultSchema;
        options.targetTable = targetTable;
        options.generateStats = false;

        // circe-be: OHDSI SQL with the schemas/cohortId inlined from options. It still
        // contains SqlRender conditionals (e.g. the now-`{0 != 0}?{...}` stats blocks).
        String ohdsiSql = builder.buildExpressionQuery(circeJson, options);
        // Collapse the remaining {condition}?{...} template syntax (stats blocks vanish).
        String rendered = SqlRender.renderSql(ohdsiSql, new String[0], new String[0]);
        // Translate the OHDSI SQL dialect to DuckDB.
        String duckdbSql = SqlTranslate.translateSql(rendered, dialect);

        System.out.print(duckdbSql);
    }

    private static String readAll(InputStream in) throws Exception {
        return new String(in.readAllBytes(), StandardCharsets.UTF_8);
    }
}
