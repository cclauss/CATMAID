import json
import networkx as nx

from itertools import combinations
from collections import defaultdict
from functools import partial

from django.db import connection
from django.http import HttpResponse

from catmaid.models import UserRole
from catmaid.control.authentication import requires_user_role
from catmaid.control.skeleton import _neuronnames

def _next_circle(skeleton_set, relations, cursor):
    """ Return a dictionary of skeleton IDs in the skeleton_set vs a dictionary of connected skeletons vs how many connections."""
    pre = relations['presynaptic_to']
    post = relations['postsynaptic_to']
    cursor.execute('''
    SELECT tc1.skeleton_id, tc1.relation_id, tc2.skeleton_id
    FROM treenode_connector tc1,
         treenode_connector tc2
    WHERE tc1.skeleton_id in (%s)
      AND tc1.connector_id = tc2.connector_id
      AND tc1.skeleton_id != tc2.skeleton_id
      AND tc1.relation_id != tc2.relation_id
      AND (tc1.relation_id = %s OR tc1.relation_id = %s)
      AND (tc2.relation_id = %s OR tc2.relation_id = %s)
    ''' % (','.join(map(str, skeleton_set)), pre, post, pre, post))
    connections = defaultdict(partial(defaultdict, partial(defaultdict, int)))
    for row in cursor.fetchall():
        connections[row[0]][row[1]][row[2]] += 1
    return connections

def _clean_mins(request, cursor, project_id):
    min_pre  = int(request.POST.get('min_pre',  -1))
    min_post = int(request.POST.get('min_post', -1))

    if -1 == min_pre and -1 == min_post:
        raise Exception("Can't grow: not retrieving any pre or post.")
    if -1 == min_pre:
        min_pre = float('inf')
    if -1 == min_post:
        min_post = float('inf')

    cursor.execute("SELECT relation_name, id FROM relation WHERE project_id = %s AND (relation_name = 'presynaptic_to' OR relation_name = 'postsynaptic_to')" % int(project_id))
    relations = dict(cursor.fetchall())
    mins = {}
    mins[relations['presynaptic_to']]  = min_post # inverted: all postsynaptic to the set
    mins[relations['postsynaptic_to']] = min_pre # inverted: all presynaptic to the set
    return mins, relations

@requires_user_role([UserRole.Annotate, UserRole.Browse])
def circles_of_hell(request, project_id=None):
    """ Given a set of one or more skeleton IDs, find all skeletons that connect
    them (n_circles=1), or that connect to others that connect them (n_circles=2), etc.
    Returns a list of unique skeleton IDs that exclude the ones provided as argument.
    """
    n_circles = int(request.POST.get('n_circles', 1))
    if n_circles < 1:
        raise Exception("Requires at least one circle.")

    first_circle = set(int(v) for k,v in request.POST.iteritems() if k.startswith('skeleton_ids['))

    if not first_circle:
        raise Exception("No skeletons were provided.")

    cursor = connection.cursor()
    mins, relations = _clean_mins(request, cursor, int(project_id))

    current_circle = first_circle
    all_circles = first_circle

    while n_circles > 0 and current_circle:
        n_circles -= 1
        connections = _next_circle(current_circle, relations, cursor)
        next_circle = set(skID for c in connections.itervalues() \
                          for relationID, cs in c.iteritems() \
                          for skID, count in cs.iteritems() if count >= mins[relationID])
        current_circle = next_circle - all_circles
        all_circles = all_circles.union(next_circle)

    skeleton_ids = tuple(all_circles - first_circle)
    return HttpResponse(json.dumps([skeleton_ids, _neuronnames(skeleton_ids, project_id)]))

@requires_user_role([UserRole.Annotate, UserRole.Browse])
def find_directed_paths(request, project_id=None):
    """ Given a set of two or more skeleton IDs, find directed paths of connected neurons between them, for a maximum inner path length as given (i.e. origin and destination not counted). A directed path means that all edges are of the same kind, e.g. presynaptic_to. """

    sources = set(int(v) for k,v in request.POST.iteritems() if k.startswith('sources['))
    targets = set(int(v) for k,v in request.POST.iteritems() if k.startswith('targets['))
    if len(sources) < 1 or len(targets) < 1:
        raise Exception('Need at least 1 skeleton IDs for both sources and targets to find directed paths!')

    path_length = int(request.POST.get('n_circles', 1))
    cursor = connection.cursor()
    mins, relations = _clean_mins(request, cursor, int(project_id))
    presynaptic_to = relations['presynaptic_to']

    def grow(graph, circle):
        s = set()
        for skid1, c in circle.iteritems():
            for relationID, targets in c.iteritems():
                threshold = mins[relationID]
                for skid2, count in targets.iteritems():
                    if count < threshold:
                        continue
                    if relationID == presynaptic_to:
                        graph.add_edge(skid1, skid2)
                    else:
                        graph.add_edge(skid2, skid2)
                    s.add(skid2)
        return s

    for source in sources:
        for target in targets:
            graph = nx.DiGraph()
            length = path_length
            s1 = set()
            t1 = set()
            s1.add(source)
            t1.add(target)
            while length > 0:
                length -= 1
                s1 = grow(graph, _next_circle(s1, relations, cursor))
                t1 = grow(graph, _next_circle(t1, relations, cursor))

    all_paths = []
    for source in sources:
        for target in targets:
            for path in nx.all_simple_paths(graph, source, target, path_length + 1):
                all_paths.append(path)

    return HttpResponse(json.dumps(all_paths))

