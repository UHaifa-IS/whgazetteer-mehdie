function url_extplace(identifier, type) {
    let link = "";  // По умолчанию пустая строка
    let identifierStr = identifier.toString();
    if (identifierStr.startsWith('http')) {
        // Обработка случая, когда идентификатор является URL
        let tag = identifierStr.replace(/.+\/\/|www.|\..+/g, '');
        link = `<a href="${identifierStr}" target="_blank">${tag}<i class="fas fa-external-link-alt" style="color: #336699;"></i></a>, `;
    } else if (identifierStr.includes(":") && identifierStr.split(":").length === 2 && type !== "closeMatch") {
        let identifierParts = identifierStr.split(":");
        let dataset = identifierParts[0];
        let place_id = identifierParts[1];
        // Строим ссылку для идентификаторов с двумя частями
        link = `<a href="/datasets/${dataset}/browse?pid=${place_id}" target="_blank">${identifier}<i class="fas fa-external-link-alt" style="color: #336699;"></i></a>, `;
    } else {
        // Используем шаблонные строки правильно
        link = `<a href="" class="ext" data-target="#ext_site">${identifier}<i class="fas fa-external-link-alt" style="color: #336699;"></i></a>, `;
    }

    return link;
}


function minmaxer(timespans) {
    //console.log('got to minmax()',JSON.stringify(timespans))
    starts = [];
    ends = []
    for (t in timespans) {
        // gets 'in', 'earliest' or 'latest'
        starts.push(Object.values(timespans[t].start)[0])
        ends.push(!!timespans[t].end ? Object.values(timespans[t].end)[0] : -1)
    }
    //console.log('starts',starts,'ends',ends)
    minmax = [Math.max.apply(null, starts), Math.max.apply(null, ends)]
    return minmax
}

// builds link for external placetype record
function url_exttype(type) {
    link = ' <a href="#" class="exttab" data-id=' + type.identifier +
        '>(' + type.label + ' {% fontawesome_icon "external-link" color="#336699" %})</a>'
    return link
}

function parsePlace(data) {
    window.featdata = data

    function onlyUnique(array) {
        const map = new Map();
        const result = [];
        for (const item of array) {
            if (!map.has(item.identifier)) {
                map.set(item.identifier, true);
                result.push({
                    identifier: item.identifier,
                    type: item.type,
                    aug: item.aug
                });
            }
        }
        return (result)
    }

    //timespan_arr = []
    //
    // TITLE
    console.log(data)
    descrip = '<p><b>MEHDIE ID</b>: <span id="row_title" class="larger text-danger">' + data.id + '</span>' + '</p>'
    descrip += '<p><b>Title</b>: <span id="row_title" class="larger text-danger">' + data.title + '</span>'
    //
    // NAME VARIANTS
    descrip += '<p class="scroll65"><b>Variants</b>: '
    for (n in data.names) {
        let name = data.names[n]
        descrip += '<p>' + name.toponym != '' ? name.toponym + '; ' : ''
    }
    //
    // TYPES
    // may include sourceLabel:"" **OR** sourceLabels[{"label":"","lang":""},...]
    // console.log('data.types',data.types)
    //{"label":"","identifier":"aat:___","sourceLabels":[{"label":" ","lang":"en"}]}
    descrip += '</p><p><b>Types</b>: '
    typeids = ''
    for (t in data.types) {
        str = ''
        var type = data.types[t]
        if ('sourceLabels' in type) {
            srclabels = type.sourceLabels
            for (l in srclabels) {
                label = srclabels[l]['label']
                str = label != '' ? label + '; ' : ''
            }
        } else if ('sourceLabel' in type) {
            str = type.sourceLabel != '' ? type.sourceLabel + '; ' : ''
        }
        if (type.label != '') {
            str += url_exttype(type) + ' '
        }
        typeids += str
    }
    descrip += typeids.replace(/(; $)/g, "") + '</p>'

    //
    // LINKS
    //
    console.log(data)
    descrip += '<p class="mb-0"><b>Links</b>: <i>original: </i>'
    close_count = added_count = related_count = 0
    html_close = html_added = html_related = ''
    if (data.links.length > 0) {
        links = data.links
        links_arr = onlyUnique(data.links)
        console.log('distinct data.links', links_arr)
        for (l in links_arr) {
            console.log('link is not different:',(links_arr[l].type !== 'different'))
            if (links_arr[l].aug === true && links_arr[l].type !== 'different') {
                added_count += 1
                html_added += url_extplace(links_arr[l].identifier, links_arr[l].type)
            } else {
                if (links_arr[l].type === 'closeMatch') {
                    close_count += 1
                    html_close += url_extplace(links_arr[l].identifier, links_arr[l].type)
                }
            }
        }
        descrip += close_count > 0 ? html_close : 'none; '
        descrip += added_count > 0 ? '<i>added</i>: ' + html_added : '<i>added</i>: none'
    } else {
        descrip += "<i class='small'>no links established yet</i>"
    }
    descrip += '</p>'

    //
    // RELATED
    //right=''
    if (data.related.length > 0) {
        parent = '<p class="mb-0"><b>Parent(s)</b>: ';
        related = '<p class="mb-0"><b>Related</b>: ';
        for (r in data.related) {
            rel = data.related[r]
            //console.log('rel',rel)
            if (rel.relation_type == 'gvp:broaderPartitive') {
                parent += '<span class="h1em">' + rel.label
                parent += 'when' in rel && !('timespans' in rel.when) ?
                    ', ' + rel.when.start.in + '-' + rel.when.end.in :
                    'when' in rel && ('timespans' in rel.when) ? ', ' +
                        minmaxer(rel.when.timespans) : ''
                //rel.when.timespans : ''
                parent += '</span>; '
            } else {
                related += '<p class="small h1em">' + rel.label + ', ' + rel.when.start.in + '-' + rel.when.end.in + '</p>'
            }
        }
        descrip += parent.length > 39 ? parent : ''
        descrip += related.length > 37 ? related : ''
    }
    //
    // DESCRIPTIONS
    // TODO: link description to identifier URI if present
    if (data.descriptions.length > 0) {
        val = data.descriptions[0]['value'].substring(0, 300)
        descrip += '<p><b>Description</b>: ' + (val.startsWith('http') ? '<a href="' + val + '" target="_blank">Link</a>' : val)
            + ' ... </p>'
        //'<br/><span class="small red-bold">('+data.descriptions[0]['identifier']+')</span>
    }
    //
    // CCODES
    //
    //if (data.ccodes.length > 0) {
    if (!!data.countries) {
        //console.log('data.countries',data.countries)
        descrip += '<p><b>Modern country bounds</b>: ' + data.countries.join(', ') + '</p>'
    }
    //
    // MINMAX
    //
    //console.log('data.minmax',data.minmax)
    mm = data.minmax
    if (data.minmax && !(mm[0] == null && mm[1] == null)) {
        descrip += '<p><b>When</b>: earliest: ' + data.minmax[0] + '; latest: ' + data.minmax[1]
    }

    // if geom(s) and 'certainty', add it
    if (data.geoms.length > 0) {
        cert = data.geoms[0].certainty
        if (cert != undefined) {
            descrip += '<p><b>Location certainty</b>: ' + cert + '</p>'
        }
    }

    // Adding the knowledge graph

    descrip += '</div>'
    return descrip
}