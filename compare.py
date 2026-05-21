# -*- coding: utf-8 -*-
"""
Created on Mon May 18 19:48:24 2026

@author: pramod.kumar
"""
from ansys.mapdl.reader import read_binary as _mapdl_read_binary
rst = _mapdl_read_binary('file.rst')

from rst_compat import read_rst_compat
rst = read_rst_compat('file.rst')
print('-----------available_results---------------')
print(rst.available_results)
print('------------n_node--------------')
print(rst.mesh.n_node)
print('-----------n_elem---------------')
print(rst.mesh.n_elem)
print('-----------nodes---------------')
print(rst.mesh.nodes[0:10])
print('-----------elem---------------')
print(rst.mesh.elem[0:10])
print('----------enum----------------')
print(rst.mesh.enum[0:10])
print('-----------nodal_stress---------------')
print(rst.nodal_stress(1))
print('------------principal_nodal_stress--------------')
print(rst.principal_nodal_stress(1))
print('-------------nodal_elastic_strain-------------')
print(rst.nodal_elastic_strain(1))
print('------------principal_nodal_stress--------------')
print(rst.principal_nodal_stress(1))