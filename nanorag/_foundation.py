# Copyright (c) 2026, Salesforce, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Embedded nanoRag Apex foundation + permset.

Bump FOUNDATION_VERSION whenever the Apex class strings or permset XML below
change. The install command returns this so users can confirm which version
of the runtime is deployed in their org.
"""

FOUNDATION_VERSION = "1.0.0"

APEX_CLASS_META_XML = r"""<?xml version="1.0" encoding="UTF-8"?>
<ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>63.0</apiVersion>
    <status>Active</status>
</ApexClass>"""

NANORAG_TOKENIZER_CLS = r"""/**
 * Shared tokenizer with stemming and bigrams for BM25 scoring.
 * Mirrors src/nanorag/tokenizer.py — parity enforced by CI test.
 */
public class NanoRagTokenizer {

    private static final Set<String> STOPWORDS = new Set<String>{
        'a','an','the','and','or','but','in','on','at','to','for','of','is','it','its',
        'this','that','with','from','by','as','be','are','was','were','been','has','have',
        'had','do','does','did','not','no','nor','so','if','all','any','each','every',
        'how','what','which','who','will','can','may','shall','would','could','should',
        'than','then','more','most','very','also','about','above','after','before',
        'between','into','through','during','under','again','further','once','here',
        'there','when','where','why','own','same','up','down','out','off','over','such',
        'few','other','some','only','just','you','your'
    };

    private static final List<String[]> SUFFIX_RULES = new List<String[]>{
        new String[]{'ies','y'}, new String[]{'ves','f'}, new String[]{'ses','s'},
        new String[]{'ches','ch'}, new String[]{'shes','sh'}, new String[]{'xes','x'},
        new String[]{'zing','z'}, new String[]{'ting','t'}, new String[]{'ning','n'},
        new String[]{'ring','r'}, new String[]{'ling','l'}, new String[]{'ping','p'},
        new String[]{'bing','b'}, new String[]{'ding','d'}, new String[]{'ging','g'},
        new String[]{'ming','m'}, new String[]{'ness',''}, new String[]{'ment',''},
        new String[]{'tion',''}, new String[]{'sion',''}, new String[]{'able',''},
        new String[]{'ible',''}, new String[]{'ally',''}, new String[]{'ical',''},
        new String[]{'ated',''}, new String[]{'ized',''}, new String[]{'iser',''},
        new String[]{'izer',''}, new String[]{'eful',''}, new String[]{'less',''},
        new String[]{'ings',''}, new String[]{'ing',''}, new String[]{'eds',''},
        new String[]{'ers',''}, new String[]{'est',''}, new String[]{'ful',''},
        new String[]{'ous',''}, new String[]{'ive',''}, new String[]{'ity',''},
        new String[]{'ed',''}, new String[]{'er',''}, new String[]{'ly',''},
        new String[]{'es',''}, new String[]{'s',''}
    };

    public static String stem(String word) {
        if (word.length() <= 3) {
            return word;
        }
        for (String[] rule : SUFFIX_RULES) {
            String suffix = rule[0];
            String replacement = rule[1];
            if (word.endsWith(suffix)) {
                Integer newLen = word.length() - suffix.length() + replacement.length();
                if (newLen >= 3) {
                    return word.substring(0, word.length() - suffix.length()) + replacement;
                }
            }
        }
        return word;
    }

    public static List<String> tokenize(String text) {
        List<String> tokens = new List<String>();
        if (String.isBlank(text)) {
            return tokens;
        }

        String lower = text.toLowerCase();
        Pattern wordPattern = Pattern.compile('[a-z][a-z0-9]*');
        Matcher m = wordPattern.matcher(lower);

        List<String> stemmed = new List<String>();
        while (m.find()) {
            String word = m.group();
            if (word.length() >= 2 && !STOPWORDS.contains(word)) {
                stemmed.add(stem(word));
            }
        }

        tokens.addAll(stemmed);

        for (Integer i = 0; i < stemmed.size() - 1; i++) {
            tokens.add(stemmed[i] + '_' + stemmed[i + 1]);
        }

        return tokens;
    }
}
"""

NANORAG_BM25_SCORER_CLS = r"""/**
 * BM25 file-level scorer. Loads bm25.json from Salesforce Files,
 * scores queries against file summaries, returns top-K source filenames.
 * Mirrors src/nanorag/scorer.py — parity enforced by CI test.
 */
public class NanoRagBM25Scorer {

    private static final Decimal K1 = 1.5;
    private static final Decimal B = 0.75;

    private Integer n;
    private Decimal avgdl;
    private Map<String, Integer> df;
    private List<DocEntry> docs;

    public class DocEntry {
        public String src;
        public Map<String, Integer> tf;
        public Integer dl;
    }

    public class ScoredResult {
        public String source;
        public Decimal score;

        public ScoredResult(String source, Decimal score) {
            this.source = source;
            this.score = score.setScale(2, RoundingMode.HALF_UP);
        }
    }

    public NanoRagBM25Scorer(String bm25Json) {
        Map<String, Object> data = (Map<String, Object>) JSON.deserializeUntyped(bm25Json);

        this.n = (Integer) data.get('n');
        this.avgdl = (Decimal) data.get('avgdl');

        this.df = new Map<String, Integer>();
        Map<String, Object> dfRaw = (Map<String, Object>) data.get('df');
        for (String key : dfRaw.keySet()) {
            this.df.put(key, (Integer) dfRaw.get(key));
        }

        this.docs = new List<DocEntry>();
        List<Object> docsRaw = (List<Object>) data.get('docs');
        for (Object docObj : docsRaw) {
            Map<String, Object> docMap = (Map<String, Object>) docObj;
            DocEntry entry = new DocEntry();
            entry.src = (String) docMap.get('src');
            entry.dl = (Integer) docMap.get('dl');
            entry.tf = new Map<String, Integer>();
            Map<String, Object> tfRaw = (Map<String, Object>) docMap.get('tf');
            for (String key : tfRaw.keySet()) {
                entry.tf.put(key, (Integer) tfRaw.get(key));
            }
            this.docs.add(entry);
        }
    }

    public List<ScoredResult> query(String text, Integer topK) {
        List<String> tokens = NanoRagTokenizer.tokenize(text);
        if (tokens.isEmpty()) {
            return new List<ScoredResult>();
        }

        List<ScoredResult> scored = new List<ScoredResult>();

        for (DocEntry doc : this.docs) {
            Decimal score = 0;
            Decimal dl = Decimal.valueOf(doc.dl);

            for (String t : tokens) {
                Integer tfVal = doc.tf.containsKey(t) ? doc.tf.get(t) : 0;
                if (tfVal == 0) {
                    continue;
                }
                Integer dfVal = this.df.containsKey(t) ? this.df.get(t) : 0;
                Decimal idf = Math.log(
                    ((Decimal)(this.n - dfVal + 0.5) / (dfVal + 0.5) + 1.0).doubleValue()
                );
                Decimal numerator = tfVal * (K1 + 1);
                Decimal denominator = tfVal + K1 * (1 - B + B * dl / this.avgdl);
                score += idf * numerator / denominator;
            }

            if (score > 0) {
                scored.add(new ScoredResult(doc.src, score));
            }
        }

        scored.sort(new ScoredResultComparator());

        List<ScoredResult> results = new List<ScoredResult>();
        for (Integer i = 0; i < Math.min(topK, scored.size()); i++) {
            results.add(scored[i]);
        }
        return results;
    }

    private class ScoredResultComparator implements Comparator<ScoredResult> {
        public Integer compare(ScoredResult a, ScoredResult b) {
            if (b.score > a.score) return 1;
            if (b.score < a.score) return -1;
            return 0;
        }
    }
}
"""

NANORAG_QUERY_SERVICE_CLS = r"""/**
 * nanoRag library search — InvocableMethod entry point for Agentforce.
 *
 * Takes a library name + user query, scores the library's BM25 index
 * via NanoRagBM25Scorer, and returns the concatenated content of the
 * top-2 matching documents for the LLM to answer over.
 *
 * Sharing model: with sharing. A library's Files are owned by the user
 * who built it (FirstPublishLocationId = UserInfo.getUserId()). Queries
 * therefore return results only for files the invoker has sharing on —
 * matches spec §13 non-goal "library owner = creator; share via copy."
 * Organizations needing system-wide libraries can wrap this class in a
 * without-sharing caller.
 */
public with sharing class NanoRagQueryService {
    @TestVisible
    private static final Integer PER_DOC_CHAR_CAP = 100000;
    @TestVisible
    private static final Integer TOP_K = 2;

    public class Input {
        @InvocableVariable(required=true) public String libraryName;
        @InvocableVariable(required=true) public String userQuery;
    }

    public class Output {
        @InvocableVariable public String fileContent;
        @InvocableVariable public List<String> sources;
        @InvocableVariable public String reasoning;
    }

    @InvocableMethod(label='Search nanoRag library')
    public static List<Output> search(List<Input> inputs) {
        List<Output> results = new List<Output>();
        // `in` is a contextual reserved word in Apex for-each — use `inp`.
        for (Input inp : inputs) {
            results.add(searchOne(inp.libraryName, inp.userQuery));
        }
        return results;
    }

    private static Output searchOne(String libraryName, String userQuery) {
        Output result = new Output();
        result.sources = new List<String>();
        result.fileContent = '';

        String indexTitle = 'nanorag/' + libraryName + '/index/bm25.json';
        List<ContentVersion> indexCv = [
            SELECT VersionData FROM ContentVersion
            WHERE Title = :indexTitle AND IsLatest = true LIMIT 1
        ];
        if (indexCv.isEmpty()) {
            result.reasoning = 'No library found with name "' + libraryName + '".';
            return result;
        }

        String indexJson = indexCv[0].VersionData.toString();
        NanoRagBM25Scorer scorer = new NanoRagBM25Scorer(indexJson);
        List<NanoRagBM25Scorer.ScoredResult> scored = scorer.query(userQuery, TOP_K);

        List<String> topFilenames = new List<String>();
        for (NanoRagBM25Scorer.ScoredResult s : scored) {
            topFilenames.add(s.source);
        }
        result.sources = topFilenames;

        if (topFilenames.isEmpty()) {
            result.reasoning = 'BM25 over ' + libraryName + ' matched 0 docs for query.';
            return result;
        }

        Set<String> titles = extractedTitles(libraryName, topFilenames);
        List<ContentVersion> docs = [
            SELECT Title, VersionData FROM ContentVersion
            WHERE Title IN :titles AND IsLatest = true
        ];

        String combined = '';
        Integer filesIncluded = 0;
        for (ContentVersion cv : docs) {
            String txt;
            try {
                txt = cv.VersionData.toString();
            } catch (System.StringException e) {
                // Non-UTF-8 content — skip this doc rather than fail the whole query.
                continue;
            }
            if (txt.length() > PER_DOC_CHAR_CAP) {
                txt = txt.substring(0, PER_DOC_CHAR_CAP) + '\n... [truncated]';
            }
            combined += cv.Title + '\n---\n' + txt + '\n\n';
            filesIncluded++;
        }
        result.fileContent = combined;
        result.reasoning = 'BM25 over ' + libraryName + ' returned ' + filesIncluded + ' of ' + topFilenames.size() + ' top doc(s).';
        return result;
    }

    private static Set<String> extractedTitles(String lib, List<String> filenames) {
        Set<String> titles = new Set<String>();
        for (String f : filenames) {
            titles.add('nanorag/' + lib + '/doc/' + f);
            titles.add('nanorag/' + lib + '/extracted/' + f + '.txt');
        }
        return titles;
    }
}
"""

NANORAG_QUERY_SERVICE_TEST_CLS = r"""@isTest
private class NanoRagQueryServiceTest {
    /**
     * Stubbed bm25.json matching NanoRagBM25Scorer's parse shape:
     * { n, avgdl, df: {term: int}, docs: [{src, dl, tf: {term: int}}] }
     *
     * Corpus (post-stem, unigrams only — bigrams omitted from stub for
     * readable math; top-K ordering is unaffected by their absence):
     *   doc1.md: "refund policy refund" -> tf {refund: 2, polici: 1}
     *   doc2.md: "shipping policy orders" -> tf {ship: 1, polici: 1, order: 1}
     */
    private static final String STUB_INDEX_JSON =
        '{'
        + '"n": 2,'
        + '"avgdl": 3,'
        + '"df": {"refund": 1, "polici": 2, "ship": 1, "order": 1},'
        + '"docs": ['
        +   '{"src": "doc1.md", "dl": 3, "tf": {"refund": 2, "polici": 1}},'
        +   '{"src": "doc2.md", "dl": 3, "tf": {"ship": 1, "polici": 1, "order": 1}}'
        + ']'
        + '}';

    @isTest
    static void search_returnsTopSourceForSingleTermQuery() {
        insertContentVersion('nanorag/test_lib/index/bm25.json', Blob.valueOf(STUB_INDEX_JSON));
        insertContentVersion('nanorag/test_lib/extracted/doc1.md.txt',
                             Blob.valueOf('refund policy refund'));

        NanoRagQueryService.Input inp = new NanoRagQueryService.Input();
        inp.libraryName = 'test_lib';
        inp.userQuery = 'refund';

        Test.startTest();
        List<NanoRagQueryService.Output> outs =
            NanoRagQueryService.search(new List<NanoRagQueryService.Input>{inp});
        Test.stopTest();

        System.assertEquals(1, outs.size(), 'One output per input expected');
        NanoRagQueryService.Output out = outs[0];
        System.assert(out.sources.contains('doc1.md'),
                      'Expected doc1.md in sources, got: ' + out.sources);
        System.assert(out.fileContent.contains('refund policy refund'),
                      'Expected doc1 extracted content in fileContent');
        System.assert(out.fileContent.contains('nanorag/test_lib/extracted/doc1.md.txt'),
                      'Expected source title header in fileContent');
        System.assert(out.reasoning.contains('BM25 over test_lib'),
                      'Expected reasoning to mention library; got: ' + out.reasoning);
    }

    @isTest
    static void search_missingIndex_returnsEmpty() {
        NanoRagQueryService.Input inp = new NanoRagQueryService.Input();
        inp.libraryName = 'nonexistent_lib';
        inp.userQuery = 'anything';

        List<NanoRagQueryService.Output> outs =
            NanoRagQueryService.search(new List<NanoRagQueryService.Input>{inp});

        System.assertEquals(1, outs.size());
        System.assertEquals('', outs[0].fileContent);
        System.assertEquals(0, outs[0].sources.size());
        System.assert(outs[0].reasoning.contains('No library found'),
                      'Expected reasoning about missing library; got: ' + outs[0].reasoning);
    }

    @isTest
    static void search_truncatesLargeDoc() {
        insertContentVersion('nanorag/test_lib/index/bm25.json', Blob.valueOf(STUB_INDEX_JSON));
        // Construct a >100k-char doc to exercise the PER_DOC_CHAR_CAP branch.
        String big = 'refund '.repeat(17143); // 7 chars × 17143 = 120_001 chars
        System.assert(big.length() > NanoRagQueryService.PER_DOC_CHAR_CAP,
                      'Test fixture must exceed cap');
        insertContentVersion('nanorag/test_lib/extracted/doc1.md.txt', Blob.valueOf(big));

        NanoRagQueryService.Input inp = new NanoRagQueryService.Input();
        inp.libraryName = 'test_lib';
        inp.userQuery = 'refund';

        List<NanoRagQueryService.Output> outs =
            NanoRagQueryService.search(new List<NanoRagQueryService.Input>{inp});

        System.assert(outs[0].fileContent.contains('... [truncated]'),
                      'Expected truncation marker in fileContent');
        System.assert(
            outs[0].fileContent.length() < NanoRagQueryService.PER_DOC_CHAR_CAP + 500,
            'fileContent should be bounded near the cap; length=' + outs[0].fileContent.length()
        );
    }

    private static void insertContentVersion(String title, Blob data) {
        ContentVersion cv = new ContentVersion(
            Title = title, PathOnClient = title,
            VersionData = data, IsMajorVersion = true
        );
        insert cv;
    }
}
"""

NANORAG_USER_PERMSET_XML = r"""<?xml version="1.0" encoding="UTF-8"?>
<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">
    <description>Allows use of the nanoRag toolkit (Apex execute on shared retrieval classes).</description>
    <label>nanoRag User</label>
    <classAccesses>
        <apexClass>NanoRagTokenizer</apexClass>
        <enabled>true</enabled>
    </classAccesses>
    <classAccesses>
        <apexClass>NanoRagBM25Scorer</apexClass>
        <enabled>true</enabled>
    </classAccesses>
    <classAccesses>
        <apexClass>NanoRagQueryService</apexClass>
        <enabled>true</enabled>
    </classAccesses>
</PermissionSet>
"""
